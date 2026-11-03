### 5.2 Labels

`project type` and `project name` are both tracked using Jira labels. Labels accept free text, so the team uses a predefined value set to keep reporting consistent. Always pick a value from the lists below instead of inventing a new one.

Each ticket should carry exactly one project type label and one project name label.

**Project type labels**

| Label | Use for |
| --- | --- |
| `server_ifwi` | Server IFWI projects |
| `client_ifwi` | Client IFWI projects |
| `graphic_ifwi` | Graphics IFWI projects |
| `bios` | BIOS projects |
| `unified_patch` | Unified patch projects |
| `cross_type` | Requirement or issue that is not specific to a single project type |

**Project name labels**

| Label | Use for |
| --- | --- |
| _(see project name table: abbreviation only)_ | The specific project the ticket belongs to |
| `all_project` | Requirement or issue that applies to all projects |

**Project name table**

Use this table to maintain the mapping between official/original project names and the Jira label abbreviation. Users must select the abbreviation value in Jira.

| Original project name | Abbreviation (label value) |
| --- | --- |
| OakStream-DiamondRapids-AP | DMR-AP |
| GraniteRapids-GemMountain | GMM |
| GraniteRapids-BirchStream-AP | GNR-AP |
| GraniteRapids-BirchStream-SP | GNR-SP |
| ClearWaterForest-BirchStream-AP | CWF-AP |
| SierraForest-BirchStream-AP | SRF-AP |
| SierraForest-BirchStream-SP | SRF-SP |
| PTL-P_IFWI | PTL-P |
| TTL-B | TTL-B |
| TTL-H | TTL-H |
| to be updated | to be updated |

**How to choose**

| Scenario | project type | project name |
| --- | --- | --- |
| Ticket belongs to one specific project | matching type, e.g. `server_ifwi` | project abbreviation from the project name table |
| Ticket applies to all projects of one type | matching type, e.g. `client_ifwi` | `all_project` |
| Ticket applies to all projects regardless of type (common feature, portal-wide behavior, infrastructure, process) | `cross_type` | `all_project` |

Do not leave these labels empty and do not create new values outside the predefined set. For project-specific tickets, always select `project name` from the abbreviation column in the project name table. If a new project is needed, add the mapping row first, then use its abbreviation.