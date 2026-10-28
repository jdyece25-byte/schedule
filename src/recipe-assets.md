# Chicken noodle soup guide

`recipe-ingredients.webp` is an original AI-generated ingredient illustration
created on 2026-10-28, encoded as WebP quality 88. Its six equal square panels
show chicken thighs, dry egg noodles, unbranded bouillon cubes, carrots,
yellow onion, and a finished soup. CSS displays each panel independently.
It is not a product photograph or a representation of a branded package;
the recipe page states this explicitly.

The recipe defaults to one serving: 120 g boneless chicken, 40 g noodles,
40 g carrots, 40 g onion, 400 mL water and 1 tsp oil. Optional two/four-serving
choices scale these amounts. Previous portion preferences migrate once to
one serving while retaining the shopping checklist. Bouillon
amounts assume one cube per US cup on the label and begin below full strength.
Chicken must reach 74°C/165°F; simmer time is only a guide.

Retail sites (QFC, Kroger, Target and Walmart) were inaccessible from the
execution environment. The page therefore makes no live price, inventory or
lowest-price claim. Retail links are searches for the named store-brand
products, not fabricated product IDs. The $14 basket is a purchasing target,
not a quotation or recipe-consumption cost. Fresh produce is unbranded.

The five-item checklist and serving choice are stored only in the browser
under `schedule-chicken-soup-v1`. The in-page countdown uses an absolute
wall-clock deadline, pauses on request and resets on a step/serving change.
It does not schedule a background alarm. No schedule data is modified.

The map selector embeds Google Maps address searches for QFC University
Village (2746 NE 45th St) and Trader Joe's University District (4555 Roosevelt
Way NE), Seattle. These are address-based candidates, not verified current
inventory or operating-status claims. Official location pages and Google
Maps were blocked by the environment's network proxy on 2026-10-28; the UI
states the verification limit and offers external map/directions links.
Directions use UW Seattle as a campus starting point, not the user's GPS.
Trader Joe's is an own-brand alternative; QFC product links remain distinct.

`recipe-steps.webp` is an original six-panel cooking illustration generated
on 2026-10-28, encoded at WebP quality 88. Panels depict chopping, sautéing,
simmering chicken, shredding, adding noodles and seasoning. The active step
selects its panel and accessible description; CSS steam animates hot dishes.
Reduced-motion preferences disable steam without removing instructional art.
The $14 figure remains a full-package purchase target, not a single meal cost.
