# Mnemolith website

A four-page static website for Mnemolith v0.1.2. No build step or dependencies to install.

## Preview

From this directory, run:

```sh
python3 -m http.server 4173 --bind 127.0.0.1
```

Open http://127.0.0.1:4173/index.html in your browser. You can also open index.html directly; keep the other files and assets beside it.

## Publish

Copy these files into your GitHub Pages publishing directory, preserving their paths:

```text
index.html
how-it-works.html
install.html
about.html
assets/
  site.css
  site.js
  plex-sans.woff2
  dashboard-light.png
  dashboard-dark.png
  stale-card-light.png
  stale-card-dark.png
  og-image.png
.nojekyll
FONT-LICENSE.txt
```

All internal links are relative, so both a custom domain and a repository subdirectory work. No rewrite rules or server-side routing are needed. The site has not been deployed as part of this delivery.

## Edit

Edit page content in the corresponding HTML file. Shared styles, interactions, and font live in assets/. design-spec.md explains the visual and interaction decisions. Review JPGs are excluded. Share metadata uses https://mnemolith.dev/; update the absolute metadata URLs on all four pages if the publishing address changes.
