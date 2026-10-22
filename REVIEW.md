# Revue initiale — api-summary

Date : 22 octobre 2026. Périmètre : nouveau service et passerelle OpenWebUI.
Revue réalisée par Codex ; identifiant exact du modèle non exposé.
Propriétaire humain proposé : @ynotopec, auteur de la demande.
Code assisté par IA ; pas d'auto-fusion ni de déploiement automatique.

## Méthode et verdict

Skills appliqués depuis `ynotopec/gouv-fr-skills` :

- `review-code`, avec références revue, checklist et modèle de PR ;
- `audit-llm-tool-identity` pour la continuité de l'identité ;
- `audit-repo-safety` pour l'ouverture du dépôt, les secrets et l'historique ;
- `fr-gov-secure-coding-rules` pour SQL, TLS, dépendances et fixtures.

Il s'agit d'une auto-revue de l'implémentation, pas d'une seconde revue humaine
indépendante. Les corrections restent dans la demande initiale de création.
Aucune modification de skill, installation de hooks tiers à l'ouverture,
réécriture d'historique ou rotation de secret.

**VERDICT : CORRIGER avant promotion en production.** Les défauts de code confirmés
ci-dessous sont corrigés et les contrôles locaux sont verts. La validation du
conteneur en CI, du SSO déployé et des volumes réels reste à réaliser. La PR est
livrée pour revue humaine ; ce rapport ne certifie pas un déploiement inexistant.

## Défauts confirmés puis corrigés

| Sévérité | Problème constaté | Correction / preuve |
|---|---|---|
| Majeure | INSERT SQLite avec un nombre de paramètres incorrect : soumission impossible. | `api_summary/store.py:169` ; tests REST et MCP `start_summary` rouges puis verts. |
| Majeure | Un échec d'un passage pouvait laisser les autres appels LLM continuer après l'échec de la tâche. | `Summarizer.batch` annule et attend les appels frères ; `test_failed_passage_cancels_sibling_requests`. |
| Majeure | Une exception à la sortie du lifespan pouvait laisser le worker actif. | Bloc `finally` dans `api_summary/app.py:93`, fermeture du moteur et du verrou. |
| Moyenne | Connexions SQLite non explicitement fermées et droits du répertoire dépendants du volume. | `Store.db` ferme la connexion ; répertoire de données en mode 0700. |
| Moyenne | Un parseur enfant héritait inutilement des secrets de l'environnement serveur. | Environnement réduit dans `Worker.extract`, limites mémoire/CPU et arrêt forcé au timeout. |
| Moyenne | Corps HTTP sans Content-Length potentiellement excessif avant validation métier. | `SecurityMiddleware` borne aussi les requêtes chunked avant parsing JSON. |

## Niveau 1 — recevabilité

| Critère | Statut | Preuve |
|---|---|---|
| Intention | Conforme | Résumer des pièces jointes OpenWebUI, avec SSO, sans URL de fichier et sans attente du résumé dans le premier appel. README et contrat. |
| Propriétaire humain | Identifié pour la revue | @ynotopec ; son acceptation finale reste humaine. |
| Preuve de comportement | Conforme localement | 31 tests, incluant API et vrai transport MCP, chiffrement/signature RSA, refus croisés et reprise. |
| Coût de revue | Réserve | Première livraison d'environ 2000 lignes Python, tests compris ; modules séparés et commits fonctionnels. |

## Niveau 2 — code et sécurité

| Contrôle | Statut | Preuve / limites |
|---|---|---|
| Logique métier partagée REST/MCP | Conforme | Deux transports délèguent aux mêmes méthodes Store, sans copie des contrôles de propriété. |
| API/SDK réels | Conforme sur versions verrouillées | Tests avec FastAPI et SDK MCP 1.30.0 ; passerelle testée avec modules internes OpenWebUI simulés. |
| Justesse des parcours asynchrones | Conforme localement | pending/running/completed/failed, consultation nouvelle session sans renvoi des fichiers, timeout et erreur de worker. |
| Transactions et reprise | Conforme localement | SQLite WAL, déduplication de soumission, blocs idempotents, réconciliation d'append interrompu, recover des jobs running. |
| SQL | Conforme | Requêtes paramétrées, aucun identifiant SQL construit depuis une entrée utilisateur. |
| TLS | Conforme par configuration production | HTTPS requis pour OIDC/JWKS, URL publique et endpoint LLM ; aucune désactivation de vérification de certificat. |
| Ressources | Bornées | Taille HTTP/blocs/fichiers, quotas propriétaire/global/file de tâches, processus d'extraction limité, concurrence LLM bornée. |
| Archives | Conforme aux tests | Refus traversée, liens/fichiers spéciaux, chiffrement, ZIP imbriqués ; limites d'expansion et nombre d'entrées. |
| Texte complet | Conforme aux tests | Dépassement du budget explicitement refusé ; fusion hiérarchique ; réponse LLM coupée par limite de tokens refusée. |
| Secrets | Aucune détection | Gitleaks 8.30.0, working tree et historique `--all`, avec `--redact`. Aucun `.env` suivi. |
| Dépendances | Aucun avis connu au moment du contrôle | `pip-audit -r requirements.lock --no-deps --disable-pip` ; versions verrouillées. Ce n'est pas une garantie d'absence de vulnérabilité. |
| Tests et lint | Conformes | 31 tests verts ; Ruff check et format check verts. Données et identités de tests synthétiques. |
| UI/DSFR/RGAA | Sans objet ici | Service API/MCP sans interface utilisateur autonome ; le front appartient à OpenWebUI. |

## Identité SSO — six sections du skill

| Section | Statut | Preuve / suites |
|---|---|---|
| 1. Passerelle → outil | Code vérifié ; déploiement non évalué | `openwebui/tool.py` réutilise `connect_mcp_server` avec l'utilisateur courant ; trois modes OAuth testés. Configuration persistée/IdP réels à contrôler. |
| 2. Surface authentifiée | Conforme au code/tests | `api_summary/auth.py:45` valide signature, issuer, audience, expiration, sub et scopes. Metadata OAuth et health publics uniquement. |
| 3. Coffre-fort par sub | Sans objet | Aucun coffre-fort utilisateur. Les fichiers et tâches sont indexés par un hash de `iss` + `sub`. La clé LLM est un secret de service. |
| 4. Visibilité OpenWebUI | Non évalué sur instance | La passerelle appelle le connecteur natif et respecte sa décision. Droits de connexion et configuration persistée restent à vérifier sur l'instance cible. |
| 5. Enrôlement coffre | Sans objet | Aucun enrôlement ni stockage d'identifiants métier par utilisateur. |
| 6. Sessions | Conforme localement ; chiffrement stockage non évalué | ContextVar isolé par requête et données privées par principal ; nouveau token vérifié à chaque connexion. Tests Alice/Bob REST et MCP. Chiffrement du volume à fournir par l'infrastructure. |

L'identité est **prouvée par JWT**, jamais déduite d'un email ou d'un header
auto-déclaré. L'isolation est démontrée dans les tests du service. Sa continuité
jusqu'au vrai IdP et à la configuration OpenWebUI n'est pas revendiquée sans
accès au déploiement.

## Sûreté d'ouverture et anti-fuite

- OUVERTURE SÛRE : oui sur le dépôt inspecté. Aucun hook actif, fsmonitor ou
  sshCommand local au départ, ni auto-exécution d'éditeur/devcontainer.
  Le build setuptools et les fixtures ont été lus avant exécution.
- ANTI-LEAK : `.gitignore`, configuration pre-commit Gitleaks, CI avec historique
  complet. Hook local Gitleaks posé dans l'environnement de travail avant livraison.
- AUCUNE FUITE DÉTECTÉE : scans Gitleaks du contenu et de l'historique existant
  sans finding. Aucun secret ou détail de production utilisé dans les fixtures
  ou les messages préparés pour les commits.
- SAFE-TO-OPEN : oui, dans le périmètre inspecté. Aucun secret à révoquer ni
  historique à réécrire n'a été identifié. Aucun hook local n'est distribué
  implicitement : l'installation pre-commit sur un autre poste reste explicite.

## Niveau 3 — statut et traçabilité

Version initiale expérimentale, explicitement marquée à valider sur l'infrastructure
cible. Propriétaire @ynotopec ; origine IA déclarée dans README, commits et PR.
Pas de contournement de revue : PR dédiée, pas d'auto-fusion.

## Contrôles restants avant production

1. Construire et démarrer l'image : environnement local sans Docker disponible ;
   la CI inclut build et smoke test health du conteneur.
2. Vérifier le vrai SSO : deux utilisateurs, audience refusée, consentement,
   refresh/expiration pendant transfert, reconnexion puis lecture de la tâche.
3. Valider la version OpenWebUI et ses interfaces internes, ainsi que les droits
   effectifs de la connexion persistée et l'accès aux originaux.
4. Tester volumes multi-Go, débit base64, espace temporaire, latence et qualité
   des résumés avec le LLM réellement choisi. Aucun benchmark n'est inventé.
5. Activer chiffrement et sauvegarde du volume, proxy HTTPS et supervision du worker.
   Le sous-processus PDF partage l'UID/volume du service : ce n'est pas une sandbox
   de sécurité. Pour des documents hostiles, une isolation dédiée est à décider.

Aucun déploiement, appel à un LLM réel, utilisation de donnée personnelle,
notification à un tiers ou validation automatique de production n'a été effectué.
