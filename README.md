# Notion → Discord

Envoie un message dans un channel Discord dès qu'une page est ajoutée dans une base Notion.
Chaque base peut aller vers un channel différent.

## Installation (5 min)

### 1. Créer l'intégration Notion
1. Va sur https://www.notion.so/profile/integrations → **New integration**
2. Type **Internal**, choisis ton espace de travail, valide
3. Copie le **Internal Integration Secret** (commence par `ntn_`)

### 2. Donner accès à ta base
Ouvre la base dans Notion → `•••` en haut à droite → **Connexions** → ajoute ton intégration.
(À faire pour chaque base surveillée.)

### 3. Créer le webhook Discord
Dans Discord : clic droit sur le channel → **Modifier le salon** → **Intégrations** → **Webhooks**
→ **Nouveau webhook** → **Copier l'URL du webhook**. Un webhook par channel.

### 4. Configurer
Copie `config.example.json` en `config.json` puis remplis :

| Champ | Obligatoire | Description |
|---|---|---|
| `notion_token` | ✅ | le secret de l'étape 1 |
| `poll_interval_seconds` | | fréquence de vérification (défaut 30, min 10) |
| `databases[].database_id` | ✅ | l'URL de la base (copier le lien) ou son ID |
| `databases[].webhook_url` | ✅ | l'URL du webhook Discord |
| `databases[].trigger` | | envoie quand la page est **validée** au lieu de quand elle est créée (voir plus bas) |
| `databases[].name` | | nom affiché en bas du message (défaut : titre de la base) |
| `databases[].color` | | couleur de la barre du message, ex. `#57F287` |
| `databases[].message` | | texte au-dessus de l'embed, ex. `@here nouvelle tâche` |
| `databases[].properties` | | colonnes à afficher, dans l'ordre (défaut : toutes celles remplies) |
| `databases[].hide_properties` | | colonnes à ne jamais afficher |
| `defaults` | | réglages communs à toutes les bases (chaque base peut les remplacer) |
| `databases[].username` / `avatar_url` | | nom et avatar du "bot" |

### Envoyer seulement les tâches validées (`trigger`)
Sans `trigger`, chaque nouvelle page est envoyée. Avec `trigger`, une page est envoyée **une seule fois**, au moment où elle devient validée :

```jsonc
"trigger": { "property": "Validé" }                              // case à cocher cochée
"trigger": { "property": "État", "value": "Done" }               // sélection ou état égal à "Done"
"trigger": { "property": "Validation", "value": "N" }            // sélection multiple contenant N
"trigger": { "property": "Validation", "value": ["N", "R", "L"] } // contenant N, R ET L
```

Si tu décoches puis recoches une tâche, elle n'est pas renvoyée.

Ajoute `"on_create": true` (dans `defaults` ou dans une base) pour **aussi** annoncer chaque nouvelle tâche dès sa création, avec le texte de `create_message` (défaut : « 🆕 Nouvelle tâche ajoutée ! »).

### Une page avec des cases à cocher (ex. planificateur)
Au lieu de `database_id`, mets `page_id` (le lien de la page). Le bot annonce chaque nouvelle case remplie
(`create_message`) et chaque case cochée (`message`), avec la rubrique (titre de la colonne) et qui l'a fait.
Une case décochée puis recochée est annoncée de nouveau.

### Plusieurs pages, plusieurs channels
Ajoute un bloc par base dans `databases`, chacun avec le webhook de son channel (Mutateur → #mutateur, etc.).

### 5. Lancer
```bash
py -m pip install -r requirements.txt
py notion_discord.py --test   # envoie la dernière page de chaque base pour vérifier
py notion_discord.py          # surveille en continu
```
(Sous Windows, utilise `py`. Sous Mac/Linux, utilise `python3`.)

Au premier lancement, les pages déjà existantes ne sont **pas** envoyées : seulement les nouvelles.

## Bon à savoir
- Délai d'envoi : environ 30 s à 2 min. Si tu crées une page vide, le script attend jusqu'à 2 min que tu tapes un titre.
- `state.json` est créé automatiquement (mémorise ce qui a déjà été envoyé) : ne le supprime pas, sinon le script repart de zéro.
- Ne partage jamais `config.json` : il contient ton token Notion et tes webhooks.

## Le faire tourner 24h/24 avec GitHub (gratuit)
Le fichier `.github/workflows/notion-discord.yml` fait lancer le script par GitHub toutes les 5 min.
1. Crée un dépôt **public** sur GitHub et mets-y `notion_discord.py`, `requirements.txt` et `.github/workflows/notion-discord.yml` (**jamais** `config.json`).
2. Settings → Secrets and variables → Actions → New repository secret : nom `CONFIG_JSON`, valeur = tout le contenu de `config.json`.
3. Actions → « Notion vers Discord » → Run workflow (coche « Mode test » pour vérifier).

Pour changer la config plus tard : modifie le secret `CONFIG_JSON`.
Les secrets ne sont jamais visibles publiquement, et les logs n'affichent pas les titres des tâches.
Si le dépôt n'a aucune activité pendant 60 jours, GitHub met en pause l'exécution automatique : il suffit de la réactiver dans l'onglet Actions.

## Autres façons de le faire tourner 24h/24
Le script doit tourner en permanence. Plusieurs options :
- **Ton PC** : ça marche tant qu'il est allumé. Sous Windows, tu peux le lancer au démarrage avec le Planificateur de tâches (`pythonw notion_discord.py` pour qu'il n'y ait pas de fenêtre).
- **Un Raspberry Pi / un petit VPS** : le plus fiable.
- **Railway, Render, Fly.io...** : hébergement gratuit ou à quelques euros/mois. Tu peux y mettre le token dans la variable d'environnement `NOTION_TOKEN` au lieu du fichier.
