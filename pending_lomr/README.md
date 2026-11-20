# Pending LOMR vs current map

A pending LOMR has been approved but is not yet effective. FEMA publishes no
GIS for it; the only source is the determination PDF. That PDF's annotated
FIRM panels are vector drawings, so the revised flood zones can be extracted
as geometry.

1. `extract_lomr_pdf.py <dir with lomr.pdf>` reads the annotated panel. It
   takes the cyan SFHA fills, the orange 0.2% fills and the heavy
   "REVISED AREA" outline, and converts page coordinates to state plane
   using the panel's grid ticks and its 1:6,000 scale.
   The page index, tick positions and outline location are set for
   Fulton LOMR 26-04-1523P (panel 13121C0369F). Adjust them for another LOMR.
2. `compare_to_current.py <same dir>` pulls the current NFHL zones around the
   site. It checks the alignment outside the revised area (best-shift
   search), then reports newly-added and removed 100-yr and 500-yr acres
   inside the revised area.

Results are labeled `status = pending`.
