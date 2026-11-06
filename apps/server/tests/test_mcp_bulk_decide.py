"""`decide_tags` over MCP: "yes to all of these except the third"."""

from sqlalchemy import select
from test_mcp import connect, env, serving  # noqa: F401 - pytest fixture

from framefound.db.models import AssetTag, Tag


async def test_claude_records_many_answers_at_once(env: dict) -> None:  # noqa: F811
    kitchen = str(env["ids"]["kitchen.jpg"])  # the learner's unjudged "silo" guess
    barn = str(env["ids"]["barn-and-silo.jpg"])  # a hand tag: must not change
    async with serving(env["app"]), connect(env["app"], env["tagger"]) as session:
        result = await session.call_tool("decide_tags", {"tag": "silo", "reject": [kitchen, barn]})
    assert not result.is_error, result.content
    body = result.structured_content
    assert (body["accepted"], body["rejected"], body["skipped"]) == (0, 1, 1)
    async with env["factory"]() as db:
        silo = (await db.execute(select(Tag).where(Tag.slug == "silo"))).scalar_one()
        rows = {
            str(a): s
            for a, s in (
                await db.execute(
                    select(AssetTag.asset_id, AssetTag.source).where(AssetTag.tag_id == silo.id)
                )
            ).all()
        }
    assert rows[kitchen] == "rejected"
    assert rows[barn] == "manual", "a hand tag is never overturned in bulk"
    assert env["learned"] == [str(silo.id)], "one re-learn for the batch"


async def test_a_read_only_token_cannot_decide_in_bulk(env: dict) -> None:  # noqa: F811
    kitchen = str(env["ids"]["kitchen.jpg"])
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool("decide_tags", {"tag": "silo", "accept": [kitchen]})
    assert result.is_error
    assert "tag" in result.content[0].text and "scope" in result.content[0].text


async def test_bad_ids_and_unknown_tags_are_explained(env: dict) -> None:  # noqa: F811
    async with serving(env["app"]), connect(env["app"], env["tagger"]) as session:
        bad_id = await session.call_tool("decide_tags", {"tag": "silo", "accept": ["nope"]})
        no_tag = await session.call_tool(
            "decide_tags", {"tag": "tractor", "accept": [str(env["ids"]["kitchen.jpg"])]}
        )
    assert bad_id.is_error and "not an asset id" in bad_id.content[0].text
    assert no_tag.is_error and "list_tags" in no_tag.content[0].text
