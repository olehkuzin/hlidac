/// One pending FHIR row of Hlidac.outbox, handed from the OutboxService to the OutboxProcess
/// (WP11). Only the row's id travels; the bundle stays in the table until it is written.
Class Hlidac.Prod.OutboxRow Extends Ens.Request
{

Property OutboxId As %String(MAXLEN = 64) [ Required ];

}
