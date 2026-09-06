"""Generate the `i-settings` gear path, both constructions, from numbers.

A gear whose teeth are individually nudged is one that cannot be redrawn at
another size later, so the teeth here are computed from a radius and an angle
step. Run it and paste the winner into `render.py`'s sprite:

    python design/render_gear.py

**The trap this exists to avoid:** `i-sun` is a circle with eight rays, and it
sits in the same toolbar. A gear drawn as a hub with six radial ticks is the same
picture. What makes a gear a gear is that the teeth sit *on a body* — so
construction A carries a body circle the sun does not have, and construction B
is a silhouette, which cannot be mistaken for anything else.
"""

from __future__ import annotations

import math

TEETH = 6
CENTRE = 8.0


def stroked(hub_r: float = 2.2, body_r: float = 4.6, tip_r: float = 6.8) -> str:
    """A: two circles and six radial strokes, on the 16 grid at 1.6px.

    Consistent with DESIGN.md 8 as written. Dense: two circles and six strokes
    inside 256 pixels, which is exactly why it has to be judged on screen.
    """
    strokes = []
    for i in range(TEETH):
        angle = math.radians(30 + i * (360 / TEETH))
        x0, y0 = CENTRE + body_r * math.cos(angle), CENTRE + body_r * math.sin(angle)
        x1, y1 = CENTRE + tip_r * math.cos(angle), CENTRE + tip_r * math.sin(angle)
        strokes.append(f"M{x0:.2f} {y0:.2f}L{x1:.2f} {y1:.2f}")
    return (
        f'<circle cx="{CENTRE:g}" cy="{CENTRE:g}" r="{hub_r:g}"/>'
        f'<circle cx="{CENTRE:g}" cy="{CENTRE:g}" r="{body_r:g}"/>'
        f'<path d="{"".join(strokes)}"/>'
    )


def filled(
    root_r: float = 4.55, tip_r: float = 6.7, hub_r: float = 2.25, tooth_deg: float = 20.0
) -> str:
    """B: one silhouette path, six teeth, the hub cut out with fill-rule evenodd.

    At 16px a filled gear is unambiguous where a stroked one turns to texture,
    which is why almost every toolbar at this size uses one.

    The outline walks root -> tip -> tip -> root around each tooth, arcing along
    the tip radius across the tooth and along the root radius through the gap, so
    the teeth are trapezoids on a round body rather than spokes from a point.
    """
    step = 360 / TEETH
    half = tooth_deg / 2
    parts: list[str] = []
    for i in range(TEETH):
        centre_deg = i * step
        a0, a1 = math.radians(centre_deg - half), math.radians(centre_deg + half)
        gap = math.radians(centre_deg + step - half)

        def point(r: float, a: float) -> str:
            return f"{CENTRE + r * math.cos(a):.2f} {CENTRE + r * math.sin(a):.2f}"

        if i == 0:
            parts.append(f"M{point(root_r, a0)}")
        parts.append(f"L{point(tip_r, a0)}")
        parts.append(f"A{tip_r:g} {tip_r:g} 0 0 1 {point(tip_r, a1)}")
        parts.append(f"L{point(root_r, a1)}")
        parts.append(f"A{root_r:g} {root_r:g} 0 0 1 {point(root_r, gap)}")
    parts.append("Z")
    # The hub, wound the other way so evenodd punches it out.
    hub = (
        f"M{CENTRE + hub_r:.2f} {CENTRE:g}"
        f"A{hub_r:g} {hub_r:g} 0 1 0 {CENTRE - hub_r:.2f} {CENTRE:g}"
        f"A{hub_r:g} {hub_r:g} 0 1 0 {CENTRE + hub_r:.2f} {CENTRE:g}Z"
    )
    return f'<path class="fill" fill-rule="evenodd" d="{"".join(parts)}{hub}"/>'


if __name__ == "__main__":
    print("A (stroked):\n" + stroked() + "\n")
    print("B (filled):\n" + filled())
