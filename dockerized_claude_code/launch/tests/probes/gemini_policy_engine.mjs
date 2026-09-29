// Drive Gemini CLI's OWN policy engine — not a re-implementation of it — for
// launch/tests/test_gemini_policy_engine.py.
//
//   node gemini_policy_engine.mjs <gemini-cli bundle dir> <spec.json> <result.json>
//
// The spec holds scenarios; each names rules files or dirs and the tier each
// loads at, and the tool calls to decide. The CLI's shipped default policies
// (<bundle>/policies) load beneath every scenario at the default tier, as the
// CLI layers them. Writes one JSON object to result.json — {"version",
// "load_errors": {scenario: [...]}, "decisions": {scenario: [...]}}, a
// decision per check, in order — to a FILE, because the engine logs every
// match to the console. WHICH rules load and WHAT each call must be decided
// live in the Python test; this file only asks the engine.
//
// The bundle's file names change per release, so the module exporting the
// engine is found by what it exports, never by name.
import { readFile, readdir, writeFile } from "node:fs/promises";
import path from "node:path";

const [bundle, specPath, resultPath] = process.argv.slice(2);
const spec = JSON.parse(await readFile(specPath, "utf-8"));

async function engineModule() {
  for (const name of (await readdir(bundle)).filter((n) => n.endsWith(".js")).sort()) {
    const file = path.join(bundle, name);
    if (!(await readFile(file, "utf-8")).includes("var PolicyEngine = class")) continue;
    const mod = await import(file);
    if (typeof mod.PolicyEngine === "function" && typeof mod.loadPoliciesFromToml === "function") return mod;
  }
  throw new Error(`no module in ${bundle} exports PolicyEngine and loadPoliciesFromToml`);
}

const { PolicyEngine, loadPoliciesFromToml } = await engineModule();
const defaults = await loadPoliciesFromToml([path.join(bundle, "policies")], () => 1);
const result = { version: null, load_errors: {}, decisions: {} };
for (const scenario of spec.scenarios) {
  const rules = [...defaults.rules];
  const checkers = [...defaults.checkers];
  const errors = [...defaults.errors];
  for (const layer of scenario.layers) {
    const loaded = await loadPoliciesFromToml([layer.path], () => layer.tier);
    rules.push(...loaded.rules);
    checkers.push(...loaded.checkers);
    errors.push(...loaded.errors);
  }
  result.load_errors[scenario.name] = errors;
  result.decisions[scenario.name] = [];
  for (const check of scenario.checks) {
    const engine = new PolicyEngine({ rules, checkers, approvalMode: check.mode });
    const { decision } = await engine.check({ name: check.tool, args: check.args }, undefined);
    result.decisions[scenario.name].push(decision);
  }
}
result.version = JSON.parse(await readFile(path.join(bundle, "..", "package.json"), "utf-8")).version;
await writeFile(resultPath, JSON.stringify(result));
