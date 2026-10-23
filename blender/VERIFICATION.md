# Verification

## Executed

Official Blender Lab source: `2cea8d566dde07fbac28a61d698909d69724e853` (v1.0.3).

| Check | Windows ARM64 | Windows x64 |
|---|---|---|
| Blender version | 5.2.2 LTS | 5.2.2 LTS |
| Isolated background add-on bridge | Pass | Pass |
| Exact package launcher, MCP initialize | Pass | Pass |
| MCP tools/list | 26 tools | 26 tools |
| Scene summary | Pass | Pass |
| Create scratch mesh and set location | Pass | Pass |
| Readback | Location `[1.5, 2.5, 3.5]`, four vertices | Same |
| Delete scratch object and confirm absent | Pass | Pass |
| Owned processes/tasks stopped; bridge port free | Confirmed independently | Confirmed independently |

Tests used factory-startup scenes and separate Blender user config/scripts/extensions directories. No existing user project or normal preferences were changed. The first ARM64 attempt had a syntax error in the test's deletion code; the server returned the error, the corrected run passed, and the leftover scratch object was removed separately.

The launcher came directly from mcp.json. Both JSON-RPC/MCP errors and the inner Blender response status were checked. This was a protocol client smoke, not an autonomous model run through the full Windows Hermes UI.

Separately on macOS: Hermes portable package loading discovered the companion skill and translated the MCP configuration; the translated launcher initialized and listed the tools. Catalog-admission validation is rerun against the catalog PR's Hermes checkout.

### Linux: Hermes opens Blender itself (0.3.0)

Ubuntu x86_64, Blender 5.2.3 LTS (official tarball on `PATH`), add-on v1.0.3 installed with `blender --command extension install-file -r user_default -e mcp-1.0.3.zip` into an isolated Blender user config whose saved **Allow Online Access** preference stays off; Blender started on a virtual X display.

| Check | Result |
|---|---|
| `blender` without `--online-mode` | Bridge never binds 9876 (40 s) |
| `blender --online-mode` | Bridge up in 3 s |
| `blender --background --online-mode --command blender_mcp` | Bridge up in 0.5 s |
| `blender --command extension list` after the CLI install | `mcp [installed]` |
| Real `hermes chat -q "What objects are in my Blender scene right now?"`, Blender closed, plugin at 0.2.0 | Agent tells the user to open Blender and enable the add-on |
| Same prompt and setup, plugin at 0.3.0 | Agent starts `blender --online-mode` itself, waits for the bridge, answers Camera/Cube/Light and says it is the startup scene (52 s, no question asked) |

The 0.3.0 run needs a Hermes whose `skill_view` resolves the `plugin:<namespace>:<skill>` form the skills index invites; on releases without it the agent may fail to load this skill by that name.

## Limits

- Background mode does not test interactive screenshots, viewport rendering or deferred completion.
- Add-on setup used a scratch profile. The skill installs the add-on through Blender's command line only after the user agrees; otherwise the user follows Blender Lab's installation instructions.
- Full Windows Hermes install/activation and profile isolation were not established by the protocol smoke alone.
- The first uv download/build took several minutes. README preparation avoids relying on a short MCP discovery deadline.
- MCP serverInfo reported `1.30.0`; the server source is identified by the immutable Git revision above, not that advertised string.
- `plugin.json` declares Blender's presence and version (5.1+, Windows and macOS). No liveness gate is declared: connection failure reports an unavailable bridge; it does not prove why Blender is unavailable.
- Presence was checked on macOS with Blender 5.2.1 in `/Applications` and on Linux with `blender` on `PATH`. The Windows locations, and the Linux flatpak, snap and Steam locations, are not yet checked on a real machine.
- Known detection gaps (the install is refused although Blender is there): a custom folder picked in the Windows MSI (the MSI records no install folder in its uninstall entry), the Microsoft Store package, a Steam library outside the default one, and a Linux tarball that is not on `PATH`.

Machine-specific raw logs and remote harnesses are retained privately and excluded from this repository.
