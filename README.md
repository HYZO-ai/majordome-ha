# Majordome 🤖🏠

**Assistant de maintenance 100 % local pour Home Assistant.** Aucune IA externe, aucun compte, aucune licence : tout tourne sur ta box.

Majordome surveille ta maison en permanence, explique les problèmes en français et propose des corrections en un clic.

## Ce qu'il fait

| | |
|---|---|
| 🩺 **Score de santé** | Note sur 100 calculée toutes les 15 min, avec la liste des points à corriger |
| 🔄 **Mises à jour sûres** | Chaque mise à jour reçoit un niveau de risque (faible / moyen / élevé / bloquée) : saut de version, mots-clés « breaking » dans les notes, version de HA requise, et pour Home Assistant Core, **les changements cassants qui touchent TES intégrations** (lus sur le blog officiel) |
| 🛟 **Installation sécurisée** | Sauvegarde → installation → vérification automatique (intégrations qui ne démarrent plus, entités devenues indisponibles). En cas de souci : alerte + bouton « revenir à la version précédente » (HACS) |
| 🧾 **Journal expliqué** | Les erreurs sont regroupées par cause (DNS, Zigbee, base de données, automatisation cassée…) avec explication et solution |
| 📡 **Zigbee2MQTT & ZHA** | Pont hors ligne, appareils déconnectés, signal faible (LQI), piles faibles, appairages incomplets, maillage trop faible, firmware du dongle, **conflit ZHA / Z2M** |
| 🐕 **Chien de garde Zigbee** | Après chaque allumage/extinction d'une lampe ou prise Zigbee, vérifie qu'elle a obéi. Sinon : renvoie la commande, réveille l'appareil et te prévient s'il reste muet |
| 🧹 **Optimisation** | Sauvegardes trop anciennes, disque plein, base trop grosse, capteurs trop bavards (avec le YAML d'exclusion prêt à copier), entités fantômes, automatisations qui pointent vers des appareils supprimés, intégrations en échec, alertes du Supervisor |
| 💬 **Assistant** | « Fais le bilan », « Quelles mises à jour sont sûres ? », « Pourquoi la lampe du salon ne marche pas ? »… Les ordres (« allume la cuisine ») sont transmis à Assist, l'agent local de Home Assistant |
| 🔔 **Notifications** | Nouveau problème critique → notification Home Assistant (+ téléphone si configuré) |

## Installation (HACS)

1. HACS → menu ⋮ → **Dépôts personnalisés** → ajoute l'URL de ce dépôt, catégorie **Intégration**.
2. Cherche **Majordome** dans HACS → **Télécharger**.
3. Redémarre Home Assistant.
4. **Paramètres → Appareils et services → Ajouter une intégration → Majordome**.

Le panneau **Majordome** apparaît dans la barre latérale (administrateurs uniquement).

### Carte pour tableau de bord

La carte est chargée automatiquement. Ajoute simplement :

```yaml
type: custom:majordome-card
```

### Réglages

Paramètres → Appareils et services → Majordome → **Configurer** :

- intervalle d'analyse (15 min par défaut)
- chien de garde Zigbee (activé par défaut)
- installation automatique des correctifs sans risque la nuit (désactivé par défaut)
- topic MQTT de Zigbee2MQTT (`zigbee2mqtt` par défaut)
- service de notification supplémentaire (ex. `notify.mobile_app_mon_iphone`)

### Capteurs

`sensor.majordome_score_de_sante`, `sensor.majordome_problemes`, `sensor.majordome_mises_a_jour`, `sensor.majordome_appareils_zigbee_hors_ligne` : utilisables dans tes automatisations.

## Sécurité

- Toute action qui modifie quelque chose (mise à jour, désactivation, suppression) demande une confirmation.
- Les fichiers supprimables se limitent aux copies de base corrompues `home-assistant_v2.db.corrupt.*`.
- Seul accès Internet : le blog officiel home-assistant.io pour lire les changements cassants.

## Licence

MIT
