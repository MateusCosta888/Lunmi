# Motion references incorporated into Lunmi

## React Bits — David Haz

Adaptations: `src/shared/RubberNavigation.tsx` (RubberSegment two-edge dilation, vertical navigation and native Web Animations instead of Motion); `src/features/chat/ThoughtLine.tsx` and `src/shared/motion.css` (ThoughtLine shimmer, breathing, timer and folding public-state trace, using Lunmi icons). These are intentionally scoped adaptations, not unchanged copies or a claim of pixel identity.

Sources, verified2026-10-15, commit43591e8b3fa327d45633b4e8239ff6742ef706ef:
- https://reactbits.dev/micro/thought-line
- https://reactbits.dev/micro/rubber-segment?radius=8
- https://github.com/DavidHDev/react-bits/tree/43591e8b3fa327d45633b4e8239ff6742ef706ef/src/ts-default/Micro

License: MIT + Commons Clause. Full notice: [ReactBits-LICENSE.md](licenses/ReactBits-LICENSE.md). Incorporated into this application only, not offered as a component pack. Native button semantics, reduced motion, wrapping, task events and cleanup were adapted.

## Aceternity UI / Vijay Verma

`src/shared/Starfield.tsx` adapts the star density/twinkle and shooting-star gradient/travel from the free components StarsBackground and ShootingStars, in one bounded Canvas. Changes: Lunmi palette, southwest trajectory, time-based speed, single occasional meteor, DPR1.5 cap, up to180stars, paint cap30fps, no per-frame React state, visibility/reduced-motion/pause and lifecycle cleanup.

Sources:
- https://ui.aceternity.com/components/shooting-stars-and-stars-background
- https://ui.aceternity.com/registry/shooting-stars.json
- https://ui.aceternity.com/registry/stars-background.json
- https://ui.aceternity.com/licence

The site permits use/modification in end products. Its source/template redistribution restrictions remain applicable. No Pro template was downloaded; this is not an MIT attribution for Aceternity.

## Lucide icons — 2026-10-15

The shared icon primitive now uses selected named imports from lucide-react1.57.0. License: ISC with Feather-derived icons under their retained MIT notice; [full packaged notice](licenses/Lucide-LICENSE.txt). References: https://lucide.dev/guide/react and https://lucide.dev/license. No icon webfont, external font request or full icon catalog is loaded at runtime. Lunmi BrandMark remains its own authored mark.
