"""Builds an attribution block for Wikipedia-sourced images bound to a cut.

Wikipedia images are frequently CC-BY-SA (requires attribution) or otherwise
carry a required credit line — engine/render/asset_sourcer.py already fetches
this via the imageinfo API and stores it on Asset.attribution/license/
license_url. Nothing appended it to the published caption until now.
"""
from api import models


def build_attribution_block(db, cut_id: int) -> str:
    """Return a compact "Image credit: ..." line for any Wikipedia-sourced
    assets bound to this cut that carry attribution text, or "" if none do.
    """
    assets = (
        db.query(models.Asset)
        .join(models.CutAsset, models.CutAsset.asset_id == models.Asset.id)
        .filter(
            models.CutAsset.cut_id == cut_id,
            models.Asset.source == "wikipedia",
            models.Asset.attribution.isnot(None),
            models.Asset.attribution != "",
        )
        .all()
    )
    if not assets:
        return ""

    credits = []
    seen = set()
    for asset in assets:
        line = asset.attribution
        if asset.license:
            line += f" ({asset.license})"
        if line in seen:
            continue
        seen.add(line)
        credits.append(line)

    return "Image credit: " + "; ".join(credits)


def build_published_caption(db, cut) -> str:
    """The exact caption text to send to a platform — cut.caption plus any
    required attribution. cut.caption itself is left untouched in the DB; the
    operator's edited caption should stay clean, only the outgoing text grows.
    """
    block = build_attribution_block(db, cut.id)
    caption = cut.caption or ""
    if not block:
        return caption
    return f"{caption}\n\n{block}" if caption else block
