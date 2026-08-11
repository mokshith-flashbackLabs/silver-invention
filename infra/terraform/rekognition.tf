# The identity collection, created HERE and never by hand.
#
# Step 4's done-when asserts the collection holds exactly as many faces as
# active `enrolments`. That assertion is meaningless if nobody can say how the
# collection came to exist — a hand-created collection is untracked, and the
# first question after a mismatch is "was this the one the CLI made in March?"
#
# INVARIANTS #4: every vector-bearing row carries model_id. The collection's
# face model version is a property of the collection, so a NEW collection is
# the only safe way to change models — never a re-index in place, which would
# leave two models' vectors side by side and every similarity between them a
# plausible-looking meaningless number. Hence the name carrying a version.

resource "aws_rekognition_collection" "identity" {
  collection_id = var.collection_id

  lifecycle {
    # Deleting this destroys every enrolled face vector in the environment.
    # Re-enrolment means every user redoing a liveness check, which is a
    # support event, not a deploy. CLAUDE.md §5: biometric enrolments are
    # expensive to recreate, which is also why the rows are soft-deleted.
    prevent_destroy = true
  }
}

output "collection_id" {
  description = "Set as REKOGNITION_COLLECTION_ID."
  value       = aws_rekognition_collection.identity.collection_id
}
