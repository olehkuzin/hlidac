/// What the OutboxProcess did with one row (WP11), shown in the Visual Trace.
/// Outcome: done | retry (still pending) | failed | skipped (no longer pending).
Class Hlidac.Prod.OutboxResult Extends Ens.Response
{

Property Outcome As %String(VALUELIST = ",done,retry,failed,skipped");

/// The row's attempts after this one (the service waits longer after each failed attempt).
Property Attempts As %Integer;

/// The error type, as stored in the row's last_error (no patient values).
Property Error As %String(MAXLEN = 200);

/// The FHIR server's HTTP status for the transaction, if it answered.
Property FhirStatus As %String(MAXLEN = 20);

}
