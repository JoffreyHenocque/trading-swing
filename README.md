# trading-swing : swing trading fictif sur le CAC 40

Chaque soir de bourse (17h45 heure de Paris en hiver), GitHub Actions :
1. télécharge les cours journaliers des 40 valeurs du CAC 40 (Yahoo Finance),
2. met à jour le compte **fictif** (achats à l'ouverture, stops, objectifs, sorties au bout de 20 séances),
3. cherche les nouveaux signaux sur la clôture du jour,
4. enregistre tout dans `data/` et le tableau de bord se met à jour.

Chaque dimanche, il refait un **backtest** des mêmes règles sur les 3 dernières années.

Aucun broker n'est connecté : rien n'est jamais acheté pour de vrai.

## Les règles (modifiables dans `config.json`)

| Réglage | Valeur | Sens |
| --- | --- | --- |
| capital | 1000 | capital fictif de départ (€) |
| risk_pct | 1 | perte maximale visée par trade : 1 % du capital |
| max_positions | 3 | positions ouvertes en même temps, au plus |
| max_position_pct | 34 | une position ne dépasse pas 34 % du capital |
| breakout_days | 20 | signal : clôture au-dessus du plus haut des 20 séances précédentes |
| trend_sma | 50 | ... et au-dessus de la moyenne des 50 dernières clôtures |
| atr_mult | 2 | stop à 2 fois l'amplitude moyenne journalière (ATR 14) sous l'entrée |
| target_r | 2 | objectif à 2 fois le risque |
| max_hold | 20 | sortie au bout de 20 séances si ni stop ni objectif |
| fee_pct / fee_min | 0,05 % / 1,50 € | frais Interactive Brokers estimés, par ordre |
| ttf_pct | 0,4 % | taxe française sur les transactions financières, à l'achat d'actions françaises |

## Fichiers

- `swing/swing.py` : le moteur (Python standard, aucune dépendance)
- `config.json`, `universe.txt` : les règles et la liste des actions
- `.github/workflows/swing.yml` : l'automatisation
- `index.html` : le tableau de bord (GitHub Pages)
- `data/` : créé automatiquement (état du compte, historique, backtest)

Paper trading uniquement. Ce n'est pas un conseil financier.
