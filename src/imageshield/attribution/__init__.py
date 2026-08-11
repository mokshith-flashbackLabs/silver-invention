"""Attribution: which enrolled person is this face in this photo?

The seeds that matter are the social-media photos screen 16 asks for. Hive is
image search — it finds *the image*, reposted or altered — so searching the
enrolment ReferenceImage (a selfie taken thirty seconds earlier that nobody
has ever reposted) finds nothing, correctly, forever. Attribution is what says
which enrolled person a photo should be a seed *for*, and without it monitoring
runs perfectly and reports nothing.

**The face is the unit, not the photo.** A photo containing the owner and a
stranger is a valid seed for the owner. A household photo with two enrolled
members produces two seeds, one each. A face matching nobody is ignored — not
an error, not a rejection, and by far the most common outcome.

This is the one module permitted to call face search (INVARIANTS #1a). The
permission is narrow and conditional: every candidate is a ``user_ref`` the
caller named, matches outside that list are discarded before they can influence
anything, no ``user_ref`` is ever created or reassigned, and a non-match is a
first-class success whose worst case is a seed not registered. None of that is
true of the enrolment path, which is why face search stays banned there.
"""
