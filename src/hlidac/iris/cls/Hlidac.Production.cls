/// The hlidac outbox production (WP11, D36). OutboxService polls Hlidac.outbox; OutboxProcess
/// checks each FHIR row with fhir_guard and records the outcome; FhirWriter, IRIS for Health's
/// own HS.FHIRServer.Interop.Operation, writes the transaction bundle into the FHIR repository
/// in-process. Emails stay with the app's outbox runner. Deployed by `python -m hlidac.iris.deploy`.
Class Hlidac.Production Extends Ens.Production
{

XData ProductionDefinition
{
<Production Name="Hlidac.Production" TestingEnabled="false" LogGeneralTraceEvents="false">
  <Description>Hlidac: outbox to FHIR (WP11)</Description>
  <ActorPoolSize>1</ActorPoolSize>
  <Item Name="OutboxService" Category="hlidac" ClassName="Hlidac.Prod.OutboxService" PoolSize="1" Enabled="true">
    <Setting Target="Adapter" Name="CallInterval">3</Setting>
  </Item>
  <Item Name="OutboxProcess" Category="hlidac" ClassName="Hlidac.Prod.OutboxProcess" PoolSize="1" Enabled="true">
  </Item>
  <Item Name="FhirWriter" Category="hlidac" ClassName="HS.FHIRServer.Interop.Operation" PoolSize="1" Enabled="true">
  </Item>
</Production>
}

}
