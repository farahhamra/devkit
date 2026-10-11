from __future__ import annotations

import json
from pathlib import Path

import pytest

from devkit.cli import main
from devkit.modules import Modules
from devkit.project import Project
from devkit.schema import Schema


@pytest.fixture
def payables_project(project):
    schema = project / "backend/prisma/schema.prisma"
    existing = schema.read_text(encoding="utf-8").replace(
        "products Product[]", "products Product[]\n bills Bill[]\n paymentsOut PaymentOut[]"
    )
    declarations = (Path(__file__).parent / "fixtures/payables.prisma.txt").read_text(encoding="utf-8")
    # Minimal counterparts complete the relationships in the supplied schema slice.
    counterparts = """
    model User { id Int @id bills Bill[] paymentsOut PaymentOut[] }
    model Supplier { id Int @id bills Bill[] paymentsOut PaymentOut[] }
    model PurchaseOrder { id Int @id bills Bill[] }
    model ChartOfAccount { id Int @id bills Bill[] }
    enum PaymentMethod { cash bank_transfer }
    """
    schema.write_text(existing + "\n" + declarations + "\n" + counterparts, encoding="utf-8")
    for module, delegate in (("bills", "bill"), ("payments-out", "paymentOut")):
        path = project / f"backend/src/core/features/{module}/{module}.service.ts"
        path.parent.mkdir(parents=True)
        path.write_text(f"export const list = () => prisma.{delegate}.findMany();", encoding="utf-8")
    return project


def invoke(project, capsys, *args):
    assert main(["--root", str(project), *args, "--json"]) == 0
    captured = capsys.readouterr()
    assert not captured.err
    result = json.loads(captured.out)
    assert not result["truncated"]
    return result["data"]


@pytest.mark.parametrize(
    "module,model,table,enum",
    [
        ("bills", "Bill", "bills", "BillStatus"),
        ("payments-out", "PaymentOut", "payments_out", "PaymentMethod"),
    ],
)
def test_new_model_module_and_physical_table_mapping(payables_project, capsys, module, model, table, enum):
    inspection = invoke(payables_project, capsys, "module", "inspect", module)
    assert inspection["models"][0]["name"] == model
    assert inspection["models"][0]["table"] == table
    assert inspection["models"][0]["module"] == [module]
    assert inspection["enums"] == [enum]
    assert inspection["missing_models"] == []
    assert any(
        f["path"].endswith(f"{module}.service.ts") and f["ownership"] == "owned" for f in inspection["files"]
    )
    listing = invoke(payables_project, capsys, "schema", "list", "--module", module)
    assert {row["name"] for row in listing} == {model, enum}
    result = invoke(payables_project, capsys, "module", "check", module)
    assert result["summary"]["fail"] == 0
    assert any(c["rule"] == "prisma_reference" and c["status"] == "pass" for c in result["checks"])


def test_bill_constraints_enum_and_payment_out_relation(payables_project, capsys):
    bill = invoke(payables_project, capsys, "schema", "inspect", "Bill")
    assert len(bill["fields"]) == 23
    assert bill["unique_constraints"][0]["fields"] == ["organizationId", "billNumber"]
    fields = {f["name"]: f for f in bill["fields"]}
    assert fields["status"]["default"] == "draft"
    assert fields["supplier"]["on_delete"] == "Restrict"
    assert fields["order"]["optional"]
    enum = invoke(payables_project, capsys, "schema", "inspect", "BillStatus")
    assert [v["name"] for v in enum["values"]] == ["draft", "sent", "paid", "partial", "cancelled"]
    payment = invoke(payables_project, capsys, "schema", "inspect", "PaymentOut")
    fields = {f["name"]: f for f in payment["fields"]}
    assert fields["paymentNumber"]["column"] == "payment_number"
    assert fields["paymentNumber"]["unique"]
    assert fields["billId"]["optional"]
    relations = invoke(payables_project, capsys, "schema", "relations", "Bill", "--direction", "incoming")
    edge = next(row for row in relations if row["source"] == "PaymentOut")
    assert edge["source_table"] == "payments_out" and edge["target_table"] == "bills"
    assert edge["foreign_keys"] == ["billId"] and edge["on_delete"] == "SetNull"
    assert edge["optional"] and edge["cardinality"] == "many-to-one"
    impact = invoke(payables_project, capsys, "module", "impact", "bills")
    assert "payments-out" in impact["affected_modules"]


def test_new_module_aliases_keep_incoming_payments_separate(payables_project):
    project = Project(payables_project)
    modules = Modules(project, Schema(project))
    assert modules.get_name("bill") == "bills"
    assert modules.get_name("payments_out") == "payments-out"
    assert modules.get_name("payment-out") == "payments-out"
    assert modules.owner("Bill", "erp") == ["bills"]
    assert modules.owner("PaymentOut", "erp") == ["payments-out"]
    assert modules.owner("Payment", "erp") == ["payments"]


def test_missing_new_models_are_not_fabricated_in_older_schema(project):
    p = Project(project)
    modules = Modules(p, Schema(p))
    for module, model in (("bills", "Bill"), ("payments-out", "PaymentOut")):
        assert modules.inspect(module)["missing_models"] == [model]
        assert not next(row for row in modules.catalog() if row["name"] == module)["available"]


def test_nested_devkit_schema_fixture_is_not_a_live_schema(project):
    fixture = Path(__file__).parent / "fixtures/payables.prisma.txt"
    nested = project / ".agents/devkit/tests/fixtures" / fixture.name
    nested.parent.mkdir(parents=True)
    nested.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    schema = Schema(Project(project))
    assert nested not in schema.paths
    assert {model["name"] for model in schema.models}.isdisjoint({"Bill", "PaymentOut"})
