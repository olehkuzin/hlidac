/// Runs src/hlidac/fhir_guard.py inside IRIS: the same file the app checks every bundle with
/// before it enqueues it. deploy.py stores its source in ^Hlidac.Code("fhir_guard"), so nothing
/// is installed in the container; each job loads it once per version.
Class Hlidac.Prod.Guard
{

/// One of {"bundle": <the exact bundle JSON the guard checked>}, {"unsafe": <reason>} (the row
/// must never be written) or {"unavailable": <reason>} (the guard itself can't run: leave the row
/// alone). Fails closed: anything unexpected is unsafe or unavailable, never a bundle.
ClassMethod Inspect(jobJson As %String) As %DynamicObject
{
	Set source = $Get(^Hlidac.Code("fhir_guard"))
	If source = "" {
		Quit {"unavailable": "GuardMissing: run python -m hlidac.iris.deploy"}
	}
	Quit ##class(%DynamicObject).%FromJSON(..Check(source, $Get(^Hlidac.Code("fhir_guard", "sha256")), jobJson))
}

ClassMethod Check(source As %String, version As %String, jobJson As %String) As %String [ Language = python ]
{
	import json
	import sys
	import types

	def strict_object(pairs):
		keys = [key for key, _ in pairs]
		if len(keys) != len(set(keys)):
			raise ValueError("duplicate key in the job")
		return dict(pairs)

	def no_constant(name):
		raise ValueError("non-finite number in the job: " + name)

	name = "hlidac_fhir_guard_" + version
	guard = sys.modules.get(name)
	if guard is None:
		try:
			guard = types.ModuleType(name)
			exec(compile(source, "fhir_guard.py", "exec"), guard.__dict__)
		except Exception as error:
			return json.dumps({"unavailable": "GuardBroken: " + type(error).__name__})
		sys.modules[name] = guard
	try:
		job = json.loads(jobJson, object_pairs_hook=strict_object, parse_constant=no_constant)
		if not isinstance(job, dict) or job.get("kind") != "fhir_transaction":
			raise ValueError("the job is not a FHIR transaction")
		bundle = job["bundle_json"]
		if not isinstance(bundle, str):
			raise ValueError("bundle_json is not a string")
		guard.check_json(bundle)
	except Exception as error:
		return json.dumps({"unsafe": type(error).__name__ + ": " + str(error)})
	return json.dumps({"bundle": bundle})
}

}
