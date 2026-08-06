"""Spike-residue check for step-4 carry-forward #2: identity-v1 must hold
zero faces before the lookalike regression test means anything.

Devtools only — the sanctioned place for real-AWS spike checks (CLAUDE.md
§8). Run: python devtools/check_collection.py [--purge]
"""

from __future__ import annotations

import argparse
from typing import Any

import boto3


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection", default="identity-v1")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--purge", action="store_true", help="DeleteFaces everything found")
    args = parser.parse_args()

    client = boto3.client("rekognition", region_name=args.region)
    try:
        faces: list[dict[str, Any]] = []
        paginator = client.get_paginator("list_faces")
        for page in paginator.paginate(CollectionId=args.collection):
            faces.extend(page["Faces"])
    except client.exceptions.ResourceNotFoundException:
        print(f"{args.collection}: collection does not exist (zero faces, trivially)")
        return

    print(f"{args.collection}: {len(faces)} faces")
    for face in faces:
        print(f"  {face['FaceId']}  ExternalImageId={face.get('ExternalImageId')}")

    if faces and args.purge:
        client.delete_faces(
            CollectionId=args.collection, FaceIds=[face["FaceId"] for face in faces]
        )
        print(f"purged {len(faces)} faces")


if __name__ == "__main__":
    main()
