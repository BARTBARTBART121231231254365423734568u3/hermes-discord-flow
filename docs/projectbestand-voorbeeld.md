# Voorbeeld: projectbestand

Zet per project een bestand in `~/.hermes/team/projects/<slug>.md`. De `<slug>` is dezelfde als de Hermes-projectslug (`hermes projects`) en als de `slug` in `projecten` van je flow-config. Bestanden die met `_` beginnen (zoals `_TEMPLATE.md`) worden overgeslagen.

De scripts lezen twee dingen uit dit bestand:

- de regel **`STATUS: ACTIEF`** (of `GEPAUZEERD` / `GESTOPT`). Zonder STATUS-regel telt het project niet mee. ACTIEF en GEPAUZEERD krijgen vragen in #vragen; #meldingen, #staging en de samenvatting gaan alleen over ACTIEF;
- de regel **`- Staging: … (branch \`staging\`), URL https://…`**: de staging-branch en -URL. Staat `staging_url` in de flow-config, dan wint die.

Optioneel: een regel `GEEN VOLGENDE STAP: <reden>` (alleen na een beslissing van de eigenaar) houdt de bordbewaking stil als het project geen open kaart heeft.

```markdown
STATUS: ACTIEF

# Voorbeeldproject

- Repo: ~/Hermes Workspace/projects/voorbeeld-project
- Staging: deploy via de hostingpartij (branch `staging`), URL https://staging.example.invalid
- Productie: alleen na een "approve" van de eigenaar

## Doel
Wat het project oplevert, in twee zinnen.
```
