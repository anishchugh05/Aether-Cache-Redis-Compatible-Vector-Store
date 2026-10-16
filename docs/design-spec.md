# Aether Cache — multi-page website

The approved mineral-green identity, IBM Plex Sans typography, and interactive memory demonstration are preserved. Real document navigation now connects four focused pages. This supersedes the original single-file requirement at the user's request.

## Pages

| Page              | Purpose                                                                                                      |
| ----------------- | ------------------------------------------------------------------------------------------------------------ |
| index.html        | Introduce the product through the memory demo, then offer paths into the site.                               |
| how-it-works.html | Explain local architecture, file-linked retirement, provenance, shared tools, and exact reported benchmarks. |
| install.html      | Cover prerequisites, installation, setup, the first memory, and diagnostic commands.                         |
| about.html        | Explain the idea, Redis-compatible origins, storage stack, and v0.1.2 status and roadmap.                                 |

Every page has its own title, description, H1, and current-page marker. The logo returns home. All four navigation links remain visible on mobile. Ordinary relative links support direct visits, refresh, new tabs, browser history, and GitHub Pages project subdirectories without routing rules.

## Visual system

- Ground #fbfcfa; surface #ffffff; ink #203b35; secondary ink #576963.
- Accent #34594f; supporting canvas #e7eee6.
- Active memories #29654a on #e8f2eb; outdated memories #a34b39 on #faede7. Status also appears in text.
- Dark ground #16221f; surface #1d2c27; ink #e9eee5; secondary ink #b0bfb4.
- Content width 1200px; mobile gutters 20px. Home stacks below 860px. Mobile navigation becomes a complete second row.
- IBM Plex Sans is self-hosted as a subsetted variable WOFF2. Monospace is reserved for code, measurements, and identifiers. FONT-LICENSE.txt accompanies the font.
- Theme preference persists across pages. Without a saved preference, the site follows the system theme.

## Motion

CSS provides 180ms navigation underline and link-arrow feedback. The browser's Web Animations API provides a 240ms page arrival after pointer navigation and a 220ms transition when demo records change. Both use the existing exponential ease-out token. Arrivals move only 6px; demo changes move 3px. There are no scroll-triggered entrances or animation dependencies.

Navigation is never intercepted or delayed. Keyboard page navigation and keyboard demo actions skip animation. Modified clicks and external links do not set an arrival effect. Reduced-motion mode disables transitions and initializes the demo to its final state without autoplay. Enabling reduced motion cancels active animations. Rapid demo updates cancel previous effects on the same element.

The illustrative demo plays once, supports pause, replay, and manual progression, and stops in a hidden tab. Screen-reader announcements explain each change. It executes no commands and transmits no data. Clipboard controls provide success/error feedback.

## Implementation

The four HTML documents share assets/site.css, assets/site.js, and assets/plex-sans.woff2. A small inline script applies the saved theme before painting. Shared JavaScript initializes the demo only when its elements exist. No framework, build step, external runtime dependencies, analytics, or backend is required. Content and navigation remain available without JavaScript.

All exact benchmark figures and product facts are retained from the supplied brief. No adoption metrics, testimonials, pricing, or performance guarantees have been invented. About expands the supplied product idea without inventing a personal founder story.

## Verification

- Visually reviewed all four desktop and mobile layouts; no horizontal page overflow at 390px or 360px.
- Clicked through all navigation destinations and verified current-page indicators, distinct titles, and theme persistence.
- Verified install copy-button success feedback and corrected the install heading hierarchy.
- Browser console inspection returned no warnings or errors.
- Validation checks every relative link and asset, fragment targets, unique IDs, one H1 per page, active navigation, and JavaScript syntax.
- Behavioral checks cover demo progression, retirement/replacement history, paused timers, keyboard input, reduced motion, page arrivals, and shared-script initialization without a demo.
- All exact supplied benchmarks remain on How it works.
- Impeccable's typography scan returned no findings. Existing palette contrast is unchanged. This is not a full accessibility certification; no Lighthouse score is claimed.

## Handoff

Upload all four HTML files plus assets/ and the font license together. README.md includes preview and publishing instructions. The ZIP contains the complete site. Review JPGs have been removed from the deliverable. Nothing has been published or pushed to GitHub.


## v0.1.2 content update

The existing identity, typography, colors, layout system, animations, and illustrative demo are preserved. A dashboard section follows the hero and precedes the existing directory. It uses the real supplied dashboard images, framed as an app window, and the existing code/copy control for `aether-cache ui`.

A native picture element selects the full dashboard on desktop and the supplied decision-card close-up at 600px and below. Dark sources follow prefers-color-scheme until the visitor chooses a site theme. That override updates both dark source media queries. Images have intrinsic half-resolution dimensions, responsive sizing, lazy loading, asynchronous decoding, and descriptive alt text. No new image animation was introduced. Supplied screenshots remain byte-for-byte unchanged.

Marketing version labels and all footers now say v0.1.2. Marketing middle-dot strings use plain language; identifiers inside the demo and technical illustrations remain intact. About places the browser dashboard in Today, and lists team memory with GitHub sign-in, the memory evaluation, and log compaction in Next. Install refers to any project folder and adds ui and add commands.

Every page has page-specific Open Graph and Twitter metadata with the supplied GitHub Pages base URL. assets/og-image.png is a 1200×630 rendering of the existing typography, product name, headline, and a crop of the real stale-memory screenshot. The PNG was created successfully; no fallback image URL is needed. It is only referenced by share metadata and does not add to normal page rendering weight.

The site root includes an empty .nojekyll. All internal navigation and asset paths remain relative. No new runtime dependencies or build step were added. Review JPGs are excluded from both the directory and ZIP. The homepage's HTML, CSS, JavaScript, font, and selected image total approximately 389 KiB uncompressed on desktop, or 283 KiB on mobile. Toggling theme may load the other image variant on demand.

Validation covers metadata values, section order, source image selection, 390px/360px overflow, copy feedback, original demo preservation, and original motion/state checks. Social previews are configured locally; their live platform appearance requires publishing the site first.
