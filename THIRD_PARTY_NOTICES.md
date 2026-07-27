# Third-Party Notices

The self-hosted Writ coordinator is licensed under **AGPL-3.0-only** (see
[`LICENSE`](./LICENSE)). It incorporates third-party material, attributed below.

> One directory of this repository ships under a *different outbound* license:
> `connectors/writ-mcp/` is **MIT** ([`LICENSE`](./connectors/writ-mcp/LICENSE)),
> because it runs inside your MCP client rather than inside the coordinator. The
> full license map is in the README's License section. That is our own code, not
> third-party material — it is noted here only so this file is never mistaken
> for the whole picture.

## How third-party code reaches this tree

| Class | Where it is declared | Vendored here? |
| --- | --- | --- |
| Python runtime dependencies | [`coordinator/requirements.txt`](./coordinator/requirements.txt) | No — resolved by `pip` at build time, each under its own license |
| Python dev / CI tooling | [`coordinator/requirements-dev.txt`](./coordinator/requirements-dev.txt) | No — CI only, never in the runtime image |
| npm dependencies | [`frontend/package.json`](./frontend/package.json) + `package-lock.json` | No — resolved by `npm ci` at build time |
| **Fonts** | bundled verbatim into `ui/` and into the stylesheet | **Yes — see below** |

Only the fonts are redistributed as bytes inside this repository and inside the
container image, so only they require a reproduced notice. Everything else is
fetched from its own registry at build time and carries its own license there.

## Fonts

Both bundled typefaces are licensed under the **SIL Open Font License,
Version 1.1**, reproduced in full below.

### Inter

Copyright 2016 The Inter Project Authors (https://github.com/rsms/inter)

Bundled through the `@fontsource/inter` npm package and served from the
coordinator's own origin (`ui/assets/inter-*.woff2`) — the UI never requests a
font from Google Fonts or any other third-party host, so an air-gapped install
renders identically and nothing phones home.

### Schibsted Grotesk

Copyright 2023 The Schibsted-Grotesk Project Authors
(https://github.com/schibsted/schibsted-grotesk)

A subset of Schibsted Grotesk 600 is embedded as a `data:` URI in
`frontend/src/index.css` under the internal family name `WritBrand`. It renders
the two letters of the `writ` wordmark that are set in type; the rest of the
mark (the square i-dot, the drawn `t`, and the Signal bar) is vector artwork,
not glyphs. Reserved Font Name terms: the subset is renamed, and is not
distributed under the name "Schibsted Grotesk".

> The Writ **name, wordmark, glyph, and tile** are trademarks of the Writ
> project and are **not** covered by the AGPL grant or by the font licenses
> above. You may run, fork, and redistribute this software; using its branding
> to identify a fork requires permission. Replace `frontend/src/components/brand/`
> and `frontend/public/favicon*` when you rebrand.

---

SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007
-----------------------------------------------------------

PREAMBLE
The goals of the Open Font License (OFL) are to stimulate worldwide
development of collaborative font projects, to support the font creation
efforts of academic and linguistic communities, and to provide a free and
open framework in which fonts may be shared and improved in partnership
with others.

The OFL allows the licensed fonts to be used, studied, modified and
redistributed freely as long as they are not sold by themselves. The
fonts, including any derivative works, can be bundled, embedded,
redistributed and/or sold with any software provided that any reserved
names are not used by derivative works. The fonts and derivatives,
however, cannot be released under any other type of license. The
requirement for fonts to remain under this license does not apply
to any document created using the fonts or their derivatives.

DEFINITIONS
"Font Software" refers to the set of files released by the Copyright
Holder(s) under this license and clearly marked as such. This may
include source files, build scripts and documentation.

"Reserved Font Name" refers to any names specified as such after the
copyright statement(s).

"Original Version" refers to the collection of Font Software components as
distributed by the Copyright Holder(s).

"Modified Version" refers to any derivative made by adding to, deleting,
or substituting -- in part or in whole -- any of the components of the
Original Version, by changing formats or by porting the Font Software to a
new environment.

"Author" refers to any designer, engineer, programmer, technical
writer or other person who contributed to the Font Software.

PERMISSION & CONDITIONS
Permission is hereby granted, free of charge, to any person obtaining
a copy of the Font Software, to use, study, copy, merge, embed, modify,
redistribute, and sell modified and unmodified copies of the Font
Software, subject to the following conditions:

1) Neither the Font Software nor any of its individual components,
in Original or Modified Versions, may be sold by itself.

2) Original or Modified Versions of the Font Software may be bundled,
redistributed and/or sold with any software, provided that each copy
contains the above copyright notice and this license. These can be
included either as stand-alone text files, human-readable headers or
in the appropriate machine-readable metadata fields within text or
binary files as long as those fields can be easily viewed by the user.

3) No Modified Version of the Font Software may use the Reserved Font
Name(s) unless explicit written permission is granted by the corresponding
Copyright Holder. This restriction only applies to the primary font name as
presented to the users.

4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font
Software shall not be used to promote, endorse or advertise any
Modified Version, except to acknowledge the contribution(s) of the
Copyright Holder(s) and the Author(s) or with their explicit written
permission.

5) The Font Software, modified or unmodified, in part or in whole,
must be distributed entirely under this license, and must not be
distributed under any other license. The requirement for fonts to
remain under this license does not apply to any document created
using the Font Software.

TERMINATION
This license becomes null and void if any of the above conditions are
not met.

DISCLAIMER
THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT
OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE
COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL
DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM
OTHER DEALINGS IN THE FONT SOFTWARE.
