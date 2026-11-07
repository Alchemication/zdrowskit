"""Physical unit-conversion constants shared across the project.

No project imports, so any module can use this without an import cycle.
"""

KM_PER_MILE: float = 1.609344
"""Kilometres in one international mile (exact by definition)."""

M_PER_FOOT: float = 0.3048
"""Metres in one international foot (exact by definition)."""

M_PER_YARD: float = 0.9144
"""Metres in one international yard (exact by definition)."""

CM_PER_INCH: float = 2.54
"""Centimetres in one international inch (exact by definition)."""

KJ_PER_KCAL: float = 4.184
"""Kilojoules in one kilocalorie (thermochemical calorie, as Apple defines it)."""

MS_PER_MPH: float = 0.44704
"""Metres per second in one mile per hour (exact by definition)."""
