import os

from kinto_http import BearerTokenAuth

from . import KintoClient as Client


def backport_records(event, context, **kwargs):
    """Backport records creations, updates and deletions from one collection to another.
    """
    server_url = event["server"]
    source_auth = (
        event.get("backport_records_source_auth")
        or os.environ["BACKPORT_RECORDS_SOURCE_AUTH"]
    )
    source_bucket = (
        event.get("backport_records_source_bucket")
        or os.environ["BACKPORT_RECORDS_SOURCE_BUCKET"]
    )
    source_collection = (
        event.get("backport_records_source_collection")
        or os.environ["BACKPORT_RECORDS_SOURCE_COLLECTION"]
    )

    dest_auth = event.get(
        "backport_records_dest_auth",
        os.getenv("BACKPORT_RECORDS_DEST_AUTH", source_auth),
    )
    dest_bucket = event.get(
        "backport_records_dest_bucket",
        os.getenv("BACKPORT_RECORDS_DEST_BUCKET", source_bucket),
    )
    dest_collection = event.get(
        "backport_records_dest_collection",
        os.getenv("BACKPORT_RECORDS_DEST_COLLECTION", source_collection),
    )

    if source_bucket == dest_bucket and source_collection == dest_collection:
        raise ValueError("Cannot copy records: destination is identical to source")

    source_client = Client(
        server_url=server_url,
        bucket=source_bucket,
        collection=source_collection,
        auth=tuple(source_auth.split(":", 1))
        if ":" in source_auth
        else BearerTokenAuth(source_auth),
    )
    dest_client = Client(
        server_url=server_url,
        bucket=dest_bucket,
        collection=dest_collection,
        auth=tuple(dest_auth.split(":", 1))
        if ":" in dest_auth
        else BearerTokenAuth(dest_auth),
    )

    source_timestamp = source_client.get_records_timestamp()
    dest_timestamp = dest_client.get_records_timestamp()
    if source_timestamp <= dest_timestamp:
        print("Records are in sync. Nothing to do.")
        return

    source_records = source_client.get_records()
    dest_records_by_id = {r["id"]: r for r in dest_client.get_records()}

    with dest_client.batch() as dest_batch:
        # Create or update the destination records.
        for r in source_records:
            dest_record = dest_records_by_id.pop(r["id"], None)
            if dest_record is None:
                dest_batch.create_record(data=r)
            elif r["last_modified"] > dest_record["last_modified"]:
                dest_batch.update_record(data=r)
        # Delete the records missing from source.
        for r in dest_records_by_id.values():
            dest_batch.delete_record(id=r["id"])

    ops_count = len(dest_batch.results())

    # If destination has signing, request review or auto-approve changes.
    server_info = dest_client.server_info()
    signer_config = server_info["capabilities"].get("signer", {})
    signer_resources = signer_config.get("resources", [])
    # Check destination collection config (sign-off required etc.)
    signed_dest = [
        r
        for r in signer_resources
        if r["source"]["bucket"] == dest_bucket
        and r["source"]["collection"] == dest_collection
    ]
    if len(signed_dest) == 0:
        # Not explicitly configured. Check if configured at bucket level?
        signed_dest = [
            r
            for r in signer_resources
            if r["source"]["bucket"] == dest_bucket
            and r["source"]["collection"] is None
        ]
    # Destination has no signature enabled. Nothing to do.
    if len(signed_dest) == 0:
        print(f"Done. {ops_count} changes applied.")
        return

    has_autoapproval = not signed_dest[0].get(
        "to_review_enabled", signer_config["to_review_enabled"]
    ) and not signed_dest[0].get(
        "group_check_enabled", signer_config["group_check_enabled"]
    )
    if has_autoapproval:
        # Approve the changes.
        dest_client.patch_collection(data={"status": "to-sign"})
        print(f"Done. {ops_count} changes applied and signed.")
    else:
        # Request review.
        dest_client.patch_collection(data={"status": "to-review"})
        print(f"Done. Requested review for {ops_count} changes.")
