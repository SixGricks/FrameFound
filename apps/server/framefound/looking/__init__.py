"""A model on this Mac looking at footage.

The Looker is a local model that is shown a photograph, or a few stills of
one video shot, and says what is there: a caption, how usable it is, and
tags in the catalogue's own words. This package is everything about that
question and what its answer becomes.

`prompt.py` is the question itself, and it is one question: Compare
(framefound/local_models) measures models on it before any of them is
trusted, and the reviews that run over the catalogue ask exactly the same
thing — so what was measured is what runs.

Whatever a model answers is text from a stranger. It is held to a strict
form, cut to length, never run, never used as a path or a file name, and
it reaches the catalogue only as a suggestion a person still has to judge.
"""

from framefound.ai.tag_review import ORIGIN_LOCAL as ORIGIN_LOCAL

# The numbers of looking, in one place.

# ORIGIN_LOCAL, above: AssetTag.origin of a suggestion a local model made.
# It is ai.tag_review's word, imported rather than repeated, so the two can
# never drift apart.

# Priorities on L1's lane (lower runs first): a review somebody asked for
# runs ahead of looking at what arrived, and a person's Test (0) ahead of both.
PRIORITY_REVIEW = 5
PRIORITY_INGEST = 9

# A running review hands the lane back this often even when nothing is
# waiting, so a job whose time has come (Tonight at 21:30) is not held up
# behind a folder of five thousand photographs.
SLICE_SECONDS = 600.0

# This many failures in a row and a review stops with the reason: a model
# that answers nothing usable would otherwise "review" a whole folder.
MAX_CONSECUTIVE_FAILURES = 5

# Looking at what arrived gives an item this many tries per build and
# question; a review tries once and a new review tries again.
INGEST_TRIES = 2

# The Tags page loads this many suggestions per tag. More pending local
# suggestions than that would bury it, and would stop the CLIP learner
# topping the tag up, so further ones are held back and counted.
MAX_LOCAL_PENDING_PER_TAG = 200
# Tags not yet in the catalogue one review may propose.
MAX_NEW_TAGS_PER_REVIEW = 30

# The make-ready job looks again this often, and gives up on clips when none
# became ready for this long and the queues that make them are empty.
PREPARE_EVERY_S = 120.0
PREPARE_STALLED_S = 6 * 3600.0

# How far a look score may move a search result: at most ten per cent, so a
# better take of the same match rises and a weaker match never overtakes.
LOOK_NUDGE = 0.10

# The kinds of local-AI job this package runs.
KINDS = ("look_folder", "look_ingest", "look_prepare")
