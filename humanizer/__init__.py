"""Rule-based text humanizer.

Rewrites text so it reads like a person typed it: de-AI-ifies the wording,
kills em dashes, loosens hyphenated compounds and sprinkles in the small
mistakes real typing has. Every transformation is expressed as an edit against
the source string, which is what lets the .docx writer keep formatting intact.
"""

from .core import (Edit, EditBuffer, Humanizer, Settings, apply_edits,
                   apply_edits_with_spans)

__version__ = "0.1.0"

__all__ = ["Edit", "EditBuffer", "Humanizer", "Settings", "apply_edits",
           "apply_edits_with_spans", "__version__"]
