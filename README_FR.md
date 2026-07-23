# FC26 AI Character Studio v1.0.1

## Lancement

1. `install_windows.bat` — installe automatiquement Python 3.11 si le PC ne le possède pas
2. `setup_windows.bat` pour choisir le code privé
3. `run_windows.bat`
4. Le Studio s’ouvre sur `http://127.0.0.1:8765`

## Moteur inclus

- vrai mesh FC26 LOD0 : 3 355 vertices et 6 334 triangles ;
- banque FBMorph exacte : 1 092 morphs ;
- 238 contrôles Cranium et 618 axes modifiables ;
- reconstruction multi-image ;
- progression réelle par crâne, yeux, nez, bouche et tissus ;
- modification manuelle de chaque axe ;
- verrouillage et réoptimisation ciblée ;
- export JSON et fiche HTML classée.

Les images sont envoyées uniquement au serveur local `127.0.0.1` et analysées en mémoire sur le PC. Elles ne sont pas enregistrées dans les projets ; seuls les points faciaux et le résultat sont conservés localement.

## Fermer le Studio

Ferme la fenêtre noire de `run_windows.bat`. Aucun service ne reste installé en arrière-plan.


## Installation automatique de Python

La version 1.0.1 détecte Python 3.11 automatiquement. S’il manque, `install_windows.bat` tente d’abord une installation utilisateur avec `winget`, puis utilise en secours l’installateur officiel Python 3.11.9 téléchargé depuis python.org.
