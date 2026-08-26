import { readdirSync, rmSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const frontendRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const outputRoot = join(frontendRoot, ".test-dist");
const testsRoot = join(frontendRoot, "tests");
const tsc = join(frontendRoot, "node_modules", "typescript", "bin", "tsc");

function run(command, args) {
  const result = spawnSync(command, args, {
    cwd: frontendRoot,
    encoding: "utf8",
    stdio: "inherit"
  });
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    process.exitCode = result.status ?? 1;
    return false;
  }
  return true;
}

rmSync(outputRoot, { force: true, recursive: true });
try {
  if (run(process.execPath, [tsc, "-p", "tsconfig.test.json"])) {
    const testFiles = readdirSync(testsRoot)
      .filter((name) => name.endsWith(".test.mjs"))
      .sort()
      .map((name) => join(testsRoot, name));
    if (!testFiles.length) {
      throw new Error("No frontend test files were found.");
    }
    run(process.execPath, ["--test", ...testFiles]);
  }
} finally {
  rmSync(outputRoot, { force: true, recursive: true });
}
