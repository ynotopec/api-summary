# api-summary

Service autonome de résumé de documents, avec **API REST**, **MCP Streamable HTTP**,
**traitements asynchrones persistants** et **SSO OAuth/OIDC**. Il reprend la logique
PDF + découpage/fusion de [ynotopec/summarize](https://github.com/ynotopec/summarize)
(commit de référence `2526888585a46263d2cca529f2a5c5a94673eb24`), sans importer
Streamlit ni dépendre de ce dépôt à l'exécution.

**Version initiale à valider sur votre infrastructure**, avant données sensibles.
Propriétaire : @ynotopec. Implémentation assistée par Codex ; revue humaine requise.

## Fonctionnement

1. L'utilisateur joint plusieurs documents ou un ZIP dans OpenWebUI.
2. La passerelle Python transmet les originaux par blocs MCP, sans URL de fichier.
3. Le serveur enregistre une tâche et retourne immédiatement `job_id` après transfert.
4. Le worker extrait le texte et résume en arrière-plan avec un endpoint LLM compatible OpenAI.
5. L'utilisateur revient consulter l'état puis lire le résultat. Le SSO est vérifié à chaque appel.

L'API et le MCP partagent le même stockage et les mêmes contrôles de propriété.
SQLite, les fichiers et les résultats résident dans `/data`. Les tâches interrompues
sont remises en attente au redémarrage ; un appel LLM peut donc être répété après
un arrêt brutal. Les tâches ne dépendent pas de la session de transport MCP.

Formats : PDF avec couche texte, TXT/MD/CSV/JSON UTF-8, DOCX (texte du corps), ZIP
contenant ces documents. Archives imbriquées refusées. Pour les PDF image, ajouter
un OCR avant ingestion : un document sans texte échoue explicitement.

## Déploiement

```bash
cp .env.example .env
# Renseigner OIDC, URL publique et endpoint LLM dans .env.
docker compose up --build -d
```

Le service écoute sur `127.0.0.1:8080` côté hôte. Placer un reverse proxy HTTPS
devant lui et le rendre accessible depuis OpenWebUI. Le LLM et le fournisseur OIDC
sont configurables ; aucun abonnement LLM ni fournisseur public n'est imposé.
Le tokenizer est téléchargé à la construction de l'image puis inclus dans celle-ci,
pour éviter ce téléchargement lors du premier résumé. Les certificats sont vérifiés.

Le conteneur fonctionne en UID 10001, sans capacité Linux, avec
`no-new-privileges`, système racine en lecture seule et volume `/data` accessible
en écriture. Le volume doit être possédé par cet UID. Prévoir de l'espace pour
les originaux, SQLite/WAL et les fichiers temporaires d'extraction.

**Une instance / un processus par volume** : un verrou empêche deux workers de
réinitialiser les mêmes tâches. Garder `uvicorn --workers 1`. Ne pas partager
SQLite sur NFS, ni multiplier les réplicas sur le même volume. Pour répartir
le traitement sur plusieurs machines, il faudra un stockage et une file adaptés.

Le Compose limite le conteneur à 2 Gio de RAM / 2 CPU et monte `/tmp` en tmpfs
256 Mio. Une très grosse entrée ZIP peut nécessiter un répertoire temporaire
sur disque : monter un volume dédié et définir `TMPDIR`, avec quota approprié.
Les limites de stockage logique n'incluent pas les copies temporaires/WAL.

## SSO / Keycloak

Configurer dans `.env` :

```dotenv
PUBLIC_BASE_URL=https://api-summary.example.org
OIDC_ISSUER=https://sso.example.org/realms/organisation
OIDC_AUDIENCE=api-summary
OIDC_SCOPES=summary:read summary:write
OPENAI_BASE_URL=https://llm.example.org/v1
OPENAI_API_MODEL=ai-summary
OPENAI_API_KEY=
```

Le serveur valide les JWT d'accès : signature asymétrique/JWKS, émetteur,
audience, expiration, sujet et scopes. La clé de propriété est dérivée de
`iss` + `sub`, jamais de l'email ni d'un header utilisateur auto-déclaré.
Il n'enregistre ni les jetons utilisateur ni les mots de passe. La clé du
LLM est un secret de service distinct, non transmis à OpenWebUI.

Dans Keycloak ou votre fournisseur : enregistrer un client adapté à OpenWebUI,
configurer ses URL de retour exactes, les scopes demandés et un mapper d'audience
qui émet `api-summary` dans les tokens d'accès. Le même realm ne suffit pas :
un token destiné seulement à OpenWebUI doit être refusé par api-summary.
Les deux scopes sont exigés par défaut pour les appels authentifiés.

Le serveur publie `/.well-known/oauth-protected-resource` et la variante `/mcp`,
et signale cette metadata dans le challenge `WWW-Authenticate`. Il s'agit d'un
**serveur de ressource** : l'authentification, les consentements, les refresh tokens
et l'enregistrement de clients restent gérés par l'IdP et OpenWebUI. Les JWT opaques
ne sont pas pris en charge ; choisir un access token JWT signé.

### Connexion dans OpenWebUI

- Type **MCP (Streamable HTTP)** ; URL `https://api-summary.example.org/mcp`.
- Auth **OAuth 2.1 (Static)** avec les informations du client ; autoriser les scopes.
- **Register Client**, sauvegarder, puis chaque utilisateur autorise l'intégration
  une fois depuis le chat. Ajuster `OAuth Resource Parameter` uniquement si l'IdP
  le demande ; certains fournisseurs nécessitent `Omit` et un mapper d'audience.
- Le mode **OAuth / System OAuth** est possible si le token SSO OpenWebUI porte
  déjà l'audience et les scopes requis. Aucun token partagé ne sert de secours.
- Définir les droits d'accès à la connexion pour les utilisateurs/groupes visés.
  Tenir compte de la configuration persistée OpenWebUI, qui peut primer sur l'env.

Pour l'upload des pièces jointes : importer [openwebui/tool.py](openwebui/tool.py)
dans **Workspace → Tools**. Renseigner `MCP_SERVER_ID` (`info.id` de la connexion,
sans préfixe) et `OPENWEBUI_URL` (backend de cette instance depuis son conteneur).
Associer la passerelle au modèle ; activer **File Upload**, désactiver **File Context**,
choisir **Function Calling: Native**. Autoriser ZIP et les tailles voulues dans les
réglages d'upload. Ne pas exposer les outils de blocs au LLM : la passerelle les
appelle directement et le base64 n'entre pas dans le contexte du modèle.

Cette passerelle utilise des interfaces internes OpenWebUI : à vérifier sur votre
version. Elle conserve l'authentification de la connexion native et refuse les
modes Bearer partagé, Session et None.

> Pour de nouvelles pièces jointes, utilise summarize_attachments une fois et
> communique job_id. Pour une tâche existante, utilise get_summary_status ou
> get_summary_result. Ne renvoie pas les fichiers et ne boucle pas pour attendre.

Aucune notification automatique : l'utilisateur consulte le résultat plus tard.
Le transfert initial reste nécessaire avant de pouvoir fermer le chat.

## Contrat REST et MCP

| Outil MCP | REST | Paramètres / résultat |
|---|---|---|
| `upload_begin` | `POST /v1/uploads` | `filename`, `size_bytes` → `upload_id` |
| `upload_chunk` | `PUT /v1/uploads/{id}/chunks` | `upload_id` (MCP), `index`, `data_base64` → `ok`, `next_index` |
| `upload_finish` | `POST /v1/uploads/{id}/finish` | `upload_id` → `file_id` |
| `start_summary` | `POST /v1/summaries` | `file_ids`, `instructions` → `job_id` ; REST 202 |
| `get_status` | `GET /v1/summaries/{id}` | `job_id` → `pending`, `running`, `completed` ou `failed` |
| `get_result` | `GET /v1/summaries/{id}/result` | `job_id` → résumé texte si terminé |

Les blocs commencent à l'index 0 et font 256 Kio au maximum par défaut. Un retry
du même bloc est accepté seulement si son empreinte correspond. La soumission est
dédupliquée par utilisateur + mêmes file_ids + mêmes instructions pendant la rétention.
Un retry complet avec de nouveaux fichiers produit une autre tâche.
Aucun outil n'accepte d'URL source, de chemin local fourni par le client ou d'identité
utilisateur auto-déclarée. Les métadonnées passent par JSON ; les originaux par blocs.
Ce contrat utilise des outils MCP ordinaires, pas l'extension MCP Tasks.

## Limites et exploitation

Toutes les limites figurent dans [.env.example](.env.example) : 2 Gio/fichier,
8 Gio réservés/utilisateur, 64 Gio au total, 20 fichiers/tâche, file de 100 tâches,
4 tâches actives/utilisateur. Le worker traite une tâche à la fois ; il parallélise
les appels de résumé de ses passages (`ASYNC_CONCURRENCY=4`).

L'extraction est limitée à 2 millions de caractères, 2000 pages PDF, 500 entrées
ZIP et 1 Gio décompressé. Elle échoue si ces limites sont dépassées : aucune fin
source n'est silencieusement abandonnée. Le tokenizer `cl100k_base` est un repère
à adapter au modèle ; les budgets de passages, instructions, sortie et fusion
doivent tenir dans sa fenêtre réelle. La fusion est hiérarchique et ne coupe pas
la liste des résumés partiels pour la faire tenir.

Les parseurs s'exécutent dans un sous-processus jetable avec mémoire/CPU bornés,
90 secondes de délai mural, et sans les secrets d'environnement du parent.
**Ce sous-processus n'est pas une sandbox de sécurité distincte** : il partage
l'UID et le volume du service. Pour des fichiers hostiles et des garanties plus
fortes, prévoir un parseur isolé par une frontière conteneur/VM dédiée.

Rétention par défaut : un jour après fin du traitement ; nettoyage toutes les
60 secondes. Les fichiers d'une tâche retenue restent disponibles, même si le
transfert est plus ancien. Le volume n'est pas chiffré par l'application : utiliser
le chiffrement du stockage, des sauvegardes et des droits appropriés.
Une tâche en cours peut durer jusqu'à `JOB_TIMEOUT=7200` secondes. La disponibilité
du worker est exposée par `/healthz`. Docker Compose ne redémarre pas à lui seul un
conteneur simplement marqué unhealthy : superviser ce signal.

## Développement et vérification

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock -r requirements-dev.lock
pip install --no-deps -e .
ruff check api_summary tests
pytest -q
# Développement local uniquement, pas d'authentification :
APP_ENV=development AUTH_ENABLED=false DATA_DIR=./data \
  uvicorn api_summary.app:create_app --factory --host 127.0.0.1 --port 8080
```

En production, les URLs publique, OIDC/JWKS et LLM doivent être HTTPS, et
l'authentification ne peut pas être désactivée. Les dépendances validées sont
épinglées dans les fichiers lock, à mettre à jour et auditer régulièrement.
La CI teste, analyse les secrets et construit l'image. Installer le hook local
avec `pre-commit install` si pre-commit est disponible.

[REVIEW.md](REVIEW.md) distingue les contrôles réalisés, les corrections et les
points non vérifiés, notamment IdP réel, image Docker et charge multi-Go.
