/// Polls Hlidac.outbox for pending FHIR rows (D7: IRIS FHIR sends no Subscription notifications)
/// and hands them to the OutboxProcess one at a time, in the order the app enqueued them (%ID:
/// one writer, D28; created_at follows the demo clock, which restarts at AS_OF). A row that is
/// still pending afterwards (a failed attempt) ends the round, so no newer row overtakes it (D33:
/// a check-in's rows are written in order), and is retried only after a growing wait:
/// RetryBaseSeconds x 2^(attempts - 1), so a FHIR outage of minutes doesn't use up MaxAttempts.
/// Email rows are left for the app's outbox runner (D40).
Class Hlidac.Prod.OutboxService Extends Ens.BusinessService
{

Parameter ADAPTER = "Ens.InboundAdapter";

Property TargetConfigName As Ens.DataType.ConfigName [ InitialExpression = "OutboxProcess" ];

/// 30, 60, 120, 240 s between the 5 attempts: about 7.5 minutes before a row fails.
Property RetryBaseSeconds As %Integer [ InitialExpression = 30 ];

/// Row id -> $ZHOROLOG when its next attempt may run (in memory; a restart retries at once).
Property NextTry As %String [ MultiDimensional ];

Parameter SETTINGS = "TargetConfigName:Basic,RetryBaseSeconds:Basic";

Method OnProcessInput(pInput As %RegisteredObject, Output pOutput As %RegisteredObject) As %Status
{
	Set tSC = $$$OK
	For {
		Set id = ..NextRow()
		Quit:id=""
		Quit:$Get(..NextTry(id), 0)>$ZHorolog
		Set request = ##class(Hlidac.Prod.OutboxRow).%New()
		Set request.OutboxId = id
		Set tSC = ..SendRequestSync(..TargetConfigName, request, .response)
		Quit:$$$ISERR(tSC)
		Quit:'$IsObject($Get(response))
		If response.Outcome = "retry" {
			Set ..NextTry(id) = $ZHorolog + (..RetryBaseSeconds * (2 ** (response.Attempts - 1)))
			Quit
		}
		Kill ..NextTry(id)
	}
	Quit tSC
}

/// The first pending FHIR row in enqueue order, or "".
ClassMethod NextRow() As %String
{
	Set sql = "SELECT TOP 1 outbox_id FROM Hlidac.outbox"
	Set sql = sql_" WHERE outbox_status = 'pending' AND job_kind = 'fhir_transaction'"
	Set sql = sql_" ORDER BY %ID"
	Set result = ##class(%SQL.Statement).%ExecDirect(, sql)
	If result.%SQLCODE < 0 {
		Throw ##class(%Exception.SQL).CreateFromSQLCODE(result.%SQLCODE, result.%Message)
	}
	Quit:result.%Next() result.%Get("outbox_id")
	Quit ""
}

}
