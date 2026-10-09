from devkit.schema import parse_schema


def test_inline_multiline_strings_and_comments():
    source = """
    // model NotARealModel { fake String }
    model Thing {
      id Int @id @default(autoincrement())
      text String @default("model { @relation(fake) }")
      price Decimal @db.Decimal(12, 2)
      tags Json?
      @@map("things")
    }
    enum Status { ACTIVE DISABLED }
    """
    parsed = parse_schema(source)
    assert len(parsed) == 2
    assert parsed[0]["table"] == "things"
    assert [f["name"] for f in parsed[0]["fields"]] == ["id", "text", "price", "tags"]
    assert parsed[0]["fields"][1]["default"] == '"model { @relation(fake) }"'
    assert [v["name"] for v in parsed[1]["values"]] == ["ACTIVE", "DISABLED"]


def test_attribute_like_text_in_strings_is_not_executed_as_schema():
    model = parse_schema('model Message { id Int @id text String @default("@map(fake)") }')[0]
    assert model["fields"][1]["column"] == "text"
    assert model["fields"][1]["default"] == '"@map(fake)"'
