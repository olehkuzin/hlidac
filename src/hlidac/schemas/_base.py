"""Base model and shared field types for every structured boundary (principle 10)."""

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class Strict(BaseModel):
    """Closed, strictly typed and immutable: unknown fields and type coercion are rejected."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    def evolve(self, **changes: object) -> Self:
        """A changed copy, validated again. Use this, never `model_copy(update=...)`, which skips
        the validators."""
        return self.model_validate(self.model_dump() | changes)


# Our own ids and hospital FHIR ids (FHIR allows [A-Za-z0-9-.]{1,64}).
Id = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")]
LinkId = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
]
Version = Annotated[str, StringConstraints(min_length=1, max_length=32)]
NonEmpty = Annotated[str, StringConstraints(min_length=1)]
Email = Annotated[str, StringConstraints(max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
# A path inside our app. No query string, so nothing sensitive can end up in a URL.
AppPath = Annotated[str, StringConstraints(max_length=200, pattern=r"^/([A-Za-z0-9_.-][A-Za-z0-9/_.-]*)?$")]
GradeValue = Annotated[int, Field(ge=1, le=5)]
Count = Annotated[int, Field(ge=0)]
Scalar = str | int | float | bool

# The answer codes of "Ano" and "Ne". Only `no` records `absent` (a quoted denial does too).
YES = "yes"
NO = "no"
# The answer code of the "Nevím" button. It always means `unknown`, never `absent`.
DONT_KNOW = "dont_know"
# The answer code of a number the patient typed into a quantity item; the number travels with it.
TYPED_VALUE = "typed_value"
