"""Unit tests for the pure-Python Tree-sitter polyglot scanner (#1077).

Covers Tier-1 non-Python languages: TypeScript/JS, Kotlin, Java, Go, and Rust,
including deterministic ImportEdge, SymbolEntry, CallEdge, and end-to-end
ProjectScanner().scan(...) integration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from menhir.infrastructure.polyglot_treesitter import is_treesitter_available
from menhir.infrastructure.project_scanner import ProjectScanner

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not is_treesitter_available(), reason="tree-sitter-language-pack not installed"),
]


def test_typescript_repo_extracts_imports_symbols_calls_and_tests(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"name": "demo-ts", "scripts": {"test": "vitest run"}}\n',
        encoding="utf-8",
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "ledger.ts").write_text(
        "export function recordEntry(account: string, cents: number): number {\n"
        "  return cents;\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "service.ts").write_text(
        'import { recordEntry } from "./ledger";\n\n'
        "export class PaymentService {\n"
        "  charge(account: string, cents: number): number {\n"
        "    return recordEntry(account, cents);\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "ledger.test.ts").write_text(
        'import { recordEntry } from "./ledger";\n\n'
        "export function testLedger(): void {\n"
        '  recordEntry("acct-1", 500);\n'
        "}\n",
        encoding="utf-8",
    )

    scan = ProjectScanner().scan(tmp_path, name="demo-ts")

    imports = {(e.source_path, e.target_path) for e in scan.imports}
    assert ("src/service.ts", "src/ledger.ts") in imports
    assert ("src/ledger.test.ts", "src/ledger.ts") in imports

    syms = {(s.file_path, s.parent, s.name, s.kind) for s in scan.symbols}
    assert ("src/ledger.ts", "", "recordEntry", "function") in syms
    assert ("src/service.ts", "", "PaymentService", "class") in syms
    assert ("src/service.ts", "PaymentService", "charge", "method") in syms

    calls = {(c.caller_path, c.callee_path) for c in scan.call_edges}
    assert ("src/service.ts::PaymentService.charge", "src/ledger.ts::recordEntry") in calls


def test_kotlin_repo_extracts_imports_symbols_and_calls(tmp_path: Path) -> None:
    (tmp_path / "build.gradle.kts").write_text("// gradle\n", encoding="utf-8")
    kt_dir = tmp_path / "src" / "main" / "kotlin" / "com" / "demo"
    kt_dir.mkdir(parents=True)
    (kt_dir / "TokenStore.kt").write_text(
        "package com.demo\n\n"
        "class TokenStore {\n"
        "    fun verify(token: String): Boolean = token.isNotEmpty()\n"
        "}\n",
        encoding="utf-8",
    )
    (kt_dir / "AuthService.kt").write_text(
        "package com.demo\n\n"
        "import com.demo.TokenStore\n\n"
        "class AuthService(private val store: TokenStore) {\n"
        "    fun authenticate(token: String): Boolean {\n"
        "        return store.verify(token)\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    scan = ProjectScanner().scan(tmp_path, name="demo-kt")

    imports = {(e.source_path, e.target_path) for e in scan.imports}
    assert (
        "src/main/kotlin/com/demo/AuthService.kt",
        "src/main/kotlin/com/demo/TokenStore.kt",
    ) in imports

    syms = {(s.file_path, s.parent, s.name, s.kind) for s in scan.symbols}
    assert ("src/main/kotlin/com/demo/TokenStore.kt", "", "TokenStore", "class") in syms
    assert ("src/main/kotlin/com/demo/TokenStore.kt", "TokenStore", "verify", "method") in syms
    assert ("src/main/kotlin/com/demo/AuthService.kt", "AuthService", "authenticate", "method") in syms

    calls = {(c.caller_path, c.callee_path) for c in scan.call_edges}
    assert (
        "src/main/kotlin/com/demo/AuthService.kt::AuthService.authenticate",
        "src/main/kotlin/com/demo/TokenStore.kt::TokenStore.verify",
    ) in calls


def test_go_repo_extracts_imports_symbols_and_calls(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/demo\n\ngo 1.22\n", encoding="utf-8")
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    (ledger_dir / "ledger.go").write_text(
        "package ledger\n\n"
        "func RecordEntry(cents int) int {\n"
        "    return cents\n"
        "}\n",
        encoding="utf-8",
    )
    (ledger_dir / "ledger_test.go").write_text(
        "package ledger\n\n"
        "import \"testing\"\n\n"
        "func TestRecordEntry(t *testing.T) {\n"
        "    _ = RecordEntry(100)\n"
        "}\n",
        encoding="utf-8",
    )
    svc_dir = tmp_path / "service"
    svc_dir.mkdir()
    (svc_dir / "service.go").write_text(
        "package service\n\n"
        'import "example.com/demo/ledger"\n\n'
        "func Charge(cents int) int {\n"
        "    return ledger.RecordEntry(cents)\n"
        "}\n",
        encoding="utf-8",
    )

    scan = ProjectScanner().scan(tmp_path, name="demo-go")

    imports = {(e.source_path, e.target_path) for e in scan.imports}
    assert ("service/service.go", "ledger/ledger.go") in imports
    assert ("ledger/ledger_test.go", "ledger/ledger.go") in imports

    syms = {(s.file_path, s.name, s.kind) for s in scan.symbols}
    assert ("ledger/ledger.go", "RecordEntry", "function") in syms
    assert ("service/service.go", "Charge", "function") in syms

    calls = {(c.caller_path, c.callee_path) for c in scan.call_edges}
    assert ("service/service.go::Charge", "ledger/ledger.go::RecordEntry") in calls


def test_rust_repo_extracts_imports_symbols_and_calls(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "ledger.rs").write_text(
        "pub fn record_entry(cents: i64) -> i64 {\n"
        "    cents\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "service.rs").write_text(
        "use crate::ledger::record_entry;\n\n"
        "pub struct Billing;\n\n"
        "impl Billing {\n"
        "    pub fn charge(&self, cents: i64) -> i64 {\n"
        "        record_entry(cents)\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    scan = ProjectScanner().scan(tmp_path, name="demo-rs")

    imports = {(e.source_path, e.target_path) for e in scan.imports}
    assert ("src/service.rs", "src/ledger.rs") in imports

    syms = {(s.file_path, s.parent, s.name, s.kind) for s in scan.symbols}
    assert ("src/ledger.rs", "", "record_entry", "function") in syms
    assert ("src/service.rs", "", "Billing", "class") in syms
    assert ("src/service.rs", "Billing", "charge", "method") in syms

    calls = {(c.caller_path, c.callee_path) for c in scan.call_edges}
    assert ("src/service.rs::Billing.charge", "src/ledger.rs::record_entry") in calls


def test_java_repo_extracts_imports_symbols_and_calls(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text("<project></project>\n", encoding="utf-8")
    jdir = tmp_path / "src" / "main" / "java" / "com" / "demo"
    jdir.mkdir(parents=True)
    (jdir / "Ledger.java").write_text(
        "package com.demo;\n\n"
        "public class Ledger {\n"
        "    public int recordEntry(int cents) { return cents; }\n"
        "}\n",
        encoding="utf-8",
    )
    (jdir / "BillingService.java").write_text(
        "package com.demo;\n\n"
        "import com.demo.Ledger;\n\n"
        "public class BillingService {\n"
        "    private final Ledger ledger = new Ledger();\n"
        "    public int charge(int cents) { return ledger.recordEntry(cents); }\n"
        "}\n",
        encoding="utf-8",
    )

    scan = ProjectScanner().scan(tmp_path, name="demo-java")

    imports = {(e.source_path, e.target_path) for e in scan.imports}
    assert (
        "src/main/java/com/demo/BillingService.java",
        "src/main/java/com/demo/Ledger.java",
    ) in imports

    syms = {(s.file_path, s.parent, s.name, s.kind) for s in scan.symbols}
    assert ("src/main/java/com/demo/Ledger.java", "Ledger", "recordEntry", "method") in syms
    assert ("src/main/java/com/demo/BillingService.java", "BillingService", "charge", "method") in syms

    calls = {(c.caller_path, c.callee_path) for c in scan.call_edges}
    assert (
        "src/main/java/com/demo/BillingService.java::BillingService.charge",
        "src/main/java/com/demo/Ledger.java::Ledger.recordEntry",
    ) in calls


def test_ts_and_go_scans_drive_affected_tests_blast_radius(tmp_path: Path) -> None:
    """Verify scanned TS and Go import edges produce exact affected_tests commands (#1077)."""
    from menhir.infrastructure.structure_queries import StructureGraphWriter

    class _MiniGraph:
        def __init__(self, scan):
            self.files = {
                f.rel_path: {
                    "role": f.role,
                    "stack_root": f.stack_root or ".",
                    "runner": f.test_runner or "",
                }
                for f in scan.files
            }
            self.imports = [(e.source_path, e.target_path) for e in scan.imports]

        def execute(self, query: str, params=None):
            params = params or {}
            q = " ".join(query.split())
            if "OPTIONAL MATCH (t)<-[:IMPORTS]-(i:Entity" in q:
                rows = []
                for tgt in sorted(set(params.get("layer") or [])):
                    meta = self.files.get(tgt)
                    if not meta:
                        continue
                    imps = [i for i, t in self.imports if t == tgt]
                    if not imps:
                        rows.append({"target": tgt, "target_role": meta["role"], "importer_path": None, "importer_role": None})
                    for imp in imps:
                        rows.append({
                            "target": tgt,
                            "target_role": meta["role"],
                            "importer_path": imp,
                            "importer_role": self.files.get(imp, {}).get("role", "file"),
                        })
                return rows
            if "RETURN f.structure_path AS path" in q and "$paths" in q:
                return [{"path": p} for p in (params.get("paths") or []) if p in self.files]
            if "RETURN t.structure_path AS path, t.structure_stack_root AS stack_root" in q:
                return [
                    {
                        "path": p,
                        "stack_root": self.files[p]["stack_root"],
                        "runner": self.files[p]["runner"],
                    }
                    for p in (params.get("paths") or [])
                    if p in self.files and self.files[p]["runner"]
                ]
            return []

    # TypeScript repo
    ts_root = tmp_path / "ts-app"
    (ts_root / "src").mkdir(parents=True)
    (ts_root / "package.json").write_text('{"scripts": {"test": "vitest run"}}\n', encoding="utf-8")
    (ts_root / "src" / "math.ts").write_text("export function add(a: number, b: number) { return a + b; }\n", encoding="utf-8")
    (ts_root / "src" / "math.test.ts").write_text('import { add } from "./math";\nadd(1, 2);\n', encoding="utf-8")
    (ts_root / "src" / "other.test.ts").write_text("export const x = 1;\n", encoding="utf-8")

    ts_scan = ProjectScanner().scan(ts_root, name="ts-app")
    ts_res = StructureGraphWriter(_MiniGraph(ts_scan)).query_affected_tests("ts-app", ["src/math.ts"])
    assert ts_res["test_files"] == ["src/math.test.ts"]
    assert "npm test -- src/math.test.ts" in ts_res["test_command"]

    # Go repo
    go_root = tmp_path / "go-app"
    (go_root / "pkg").mkdir(parents=True)
    (go_root / "go.mod").write_text("module example.com/goapp\n\ngo 1.22\n", encoding="utf-8")
    (go_root / "pkg" / "calc.go").write_text("package pkg\nfunc Add(a, b int) int { return a + b }\n", encoding="utf-8")
    (go_root / "pkg" / "calc_test.go").write_text('package pkg\nimport "testing"\nfunc TestAdd(t *testing.T) { _ = Add(1, 2) }\n', encoding="utf-8")

    go_scan = ProjectScanner().scan(go_root, name="go-app")
    go_res = StructureGraphWriter(_MiniGraph(go_scan)).query_affected_tests("go-app", ["pkg/calc.go"])
    assert go_res["test_files"] == ["pkg/calc_test.go"]
    assert "go test ./pkg" in go_res["test_command"]
