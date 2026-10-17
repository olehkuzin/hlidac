/// Writes one outbox row to the FHIR repository (WP11). It checks the bundle again with
/// fhir_guard, posts exactly the JSON the guard checked, in-process, through IRIS for Health's
/// HS.FHIRServer.Interop.Operation (FhirTarget), and records the outcome in the row's status
/// columns, as the outbox contract says (schemas/outbox.py): done; or attempts + 1 and the error
/// type, and failed after MaxAttempts. An unsafe bundle is never posted: its row fails at once.
/// If the guard itself can't run, the row is left as it is. last_error holds only the error type
/// and HTTP status; the details stay in the Visual Trace and the event log.
Class Hlidac.Prod.OutboxProcess Extends Ens.BusinessProcess
{

Property FhirTarget As Ens.DataType.ConfigName [ InitialExpression = "FhirWriter" ];

/// The FHIR endpoint's URL path; the operation finds the FHIR service by it.
Property FhirEndpoint As %String [ InitialExpression = "/fhir/r4" ];

/// Must equal MAX_ATTEMPTS in schemas/outbox.py (tests/test_iris_production_files.py).
Property MaxAttempts As %Integer [ InitialExpression = 5 ];

Parameter SETTINGS = "FhirTarget:Basic,FhirEndpoint:Basic,MaxAttempts:Basic";

Method OnRequest(pRequest As Hlidac.Prod.OutboxRow, Output pResponse As Hlidac.Prod.OutboxResult) As %Status
{
	Set pResponse = ##class(Hlidac.Prod.OutboxResult).%New()
	Set id = pRequest.OutboxId
	Set sql = "SELECT job_json, attempts FROM Hlidac.outbox"
	Set sql = sql_" WHERE outbox_id = ? AND outbox_status = 'pending'"
	Set row = ##class(%SQL.Statement).%ExecDirect(, sql, id)
	If row.%SQLCODE < 0 {
		Quit $$$ERROR($$$GeneralError, "SQLCODE "_row.%SQLCODE)
	}
	If 'row.%Next() {
		Set pResponse.Outcome = "skipped"
		Quit $$$OK
	}
	Set attempts = row.%Get("attempts") + 1
	Set (stream, fhirResponse) = ""
	Try {
		Set verdict = ##class(Hlidac.Prod.Guard).Inspect(row.%Get("job_json"))
		If verdict.%IsDefined("unavailable") {
			// Not the row's fault: leave it pending and uncounted; the event log says why.
			Set tSC = $$$ERROR($$$GeneralError, "fhir_guard can't run: "_verdict.unavailable)
		} ElseIf 'verdict.%IsDefined("bundle") {
			$$$LOGWARNING("Outbox row "_id_" refused by fhir_guard: "_verdict.unsafe)
			Set tSC = ..Record(id, "failed", attempts, "UnsafeBundle", pResponse)
		} Else {
			Set tSC = ..Post(id, attempts, verdict.bundle, pResponse, .stream, .fhirResponse)
		}
	} Catch error {
		$$$LOGERROR("Outbox row "_id_": "_error.DisplayString())
		Set outcome = $Select(attempts >= ..MaxAttempts: "failed", 1: "pending")
		Set tSC = ..Record(id, outcome, attempts, "ProcessError: "_error.Name, pResponse)
	}
	If $IsObject(stream) {
		Do stream.Clear()
	}
	If $IsObject(fhirResponse) && (fhirResponse.QuickStreamId '= "") {
		Do ##class(HS.SDA3.QuickStream).%DeleteId(fhirResponse.QuickStreamId)
	}
	Quit tSC
}

/// Posts the checked bundle as a FHIR transaction and records the outcome.
Method Post(id As %String, attempts As %Integer, bundle As %String, response As Hlidac.Prod.OutboxResult, ByRef stream As HS.SDA3.QuickStream, ByRef fhirResponse As HS.FHIRServer.Interop.Response) As %Status
{
	Set stream = ##class(HS.SDA3.QuickStream).%New()
	$$$ThrowOnError(stream.Write(bundle))
	$$$ThrowOnError(stream.Flush())  // Write buffers in memory; FhirWriter reads it from another job
	Set fhir = ##class(HS.FHIRServer.Interop.Request).%New()
	Set fhir.QuickStreamId = stream.%Id()
	Set fhir.Request.RequestMethod = "POST"
	Set fhir.Request.RequestPath = ""
	Set fhir.Request.RequestFormatCode = "JSON"
	Set fhir.Request.ResponseFormatCode = "JSON"
	Set fhir.Request.SessionApplication = ..FhirEndpoint
	Set tSC = ..SendRequestSync(..FhirTarget, fhir, .fhirResponse)

	Set status = $Select($IsObject($Get(fhirResponse)): fhirResponse.Response.Status, 1: "")
	Set response.FhirStatus = status
	If $$$ISOK(tSC) && (+status >= 200) && (+status < 300) {
		Quit ..Record(id, "done", attempts - 1, "", response)
	}
	Set error = $Select($$$ISERR(tSC): "FhirError: "_$System.Status.GetErrorCodes(tSC), 1: "FhirStatus: "_status)
	Set outcome = $Select(attempts >= ..MaxAttempts: "failed", 1: "pending")
	Quit ..Record(id, outcome, attempts, error, response)
}

/// Updates only the row's status columns, and only while it is pending. On success attempts and
/// last_error stay as they were, like the app's runner (outbox_runner.py).
Method Record(id As %String, status As %String, attempts As %Integer, error As %String, response As Hlidac.Prod.OutboxResult) As %Status
{
	Set response.Outcome = $Case(status, "pending": "retry", : status)
	Set response.Attempts = attempts
	Set response.Error = error
	Set statement = ##class(%SQL.Statement).%New()
	Set statement.%SelectMode = 1
	If status = "pending" {
		Set sql = "UPDATE Hlidac.outbox SET attempts = ?, last_error = ?"
		Set sql = sql_" WHERE outbox_id = ? AND outbox_status = 'pending'"
		Set args = 3, args(1) = attempts, args(2) = error, args(3) = id
	} ElseIf status = "done" {
		Set sql = "UPDATE Hlidac.outbox SET outbox_status = 'done', processed_at = ?"
		Set sql = sql_" WHERE outbox_id = ? AND outbox_status = 'pending'"
		Set args = 2, args(1) = ..NowUtc(), args(2) = id
	} Else {
		Set sql = "UPDATE Hlidac.outbox SET outbox_status = 'failed', attempts = ?, last_error = ?,"
		Set sql = sql_" processed_at = ? WHERE outbox_id = ? AND outbox_status = 'pending'"
		Set args = 4, args(1) = attempts, args(2) = error, args(3) = ..NowUtc(), args(4) = id
	}
	Set tSC = statement.%Prepare(sql)
	Quit:$$$ISERR(tSC) tSC
	Set result = statement.%Execute(args...)
	If result.%SQLCODE < 0 {
		Quit $$$ERROR($$$GeneralError, "SQLCODE "_result.%SQLCODE_" updating outbox row "_id)
	}
	Quit $$$OK
}

/// UTC, ODBC format with microseconds (the table's timestamps are UTC, schema.sql).
ClassMethod NowUtc() As %String
{
	Quit $ZDateTime($ZTimeStamp, 3, 1, 6)
}

}
