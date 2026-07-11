# SenGestion — Design System (contrainte partagée pour tous les agents)

> ⚠️ CHARTE GRAPHIQUE IMPOSÉE (examen RNCP) — à respecter STRICTEMENT.
> Aucune liberté sur les couleurs et la typographie. Le raffinement passe par
> la mise en page, l'espacement, la hiérarchie et les micro-détails.

## Couleurs (les 3 + dérivées accessibles)
```css
--marine:      #021A3D;   /* principal : texte, fonds foncés, nav */
--marine-700:  #04264f;   /* hover marine */
--or:          #F2B10E;   /* accent : FOND & boutons uniquement */
--or-600:      #d99e0b;   /* hover or */
--or-text:     #8F6600;   /* or accessible pour TEXTE sur clair (4.5:1) */
--jaune-pale:  #E8E7A2;   /* fonds clairs, bandeaux doux */
--bg:          #F9FAFB;   /* fond app */
--surface:     #FFFFFF;   /* cartes */
--ink:         #021A3D;   /* texte principal */
--muted:       #4A554F;   /* texte secondaire (>=7:1) */
--line:        #D9DCDB;   /* bordures */
--success:#145A32; --danger:#8B2500;
```

## RÈGLE D'OR RGAA (non négociable)
- L'or `#F2B10E` est une couleur de **FOND**. Texte dessus = **marine** (9.09:1 ✅).
- **JAMAIS** de texte jaune sur fond clair → utiliser `--or-text` (#8F6600).
- Tout texte doit atteindre **≥ 4.5:1**.

## Typographie
- **Titres / display** : `'Palatino', 'Palatino Linotype', 'Book Antiqua', Georgia, serif`
- **Texte / UI** : `Arial, Helvetica, sans-serif`
- Échelle : h1 32px, h2 24px, h3 18px, body 16px, small 14px.

## Composants (cohérence obligatoire)
- Rayons : boutons/inputs 8px, cartes 12px.
- Bouton primaire : fond marine, texte blanc.
- Bouton accent : fond or, texte marine.
- Ombres douces : `0 1px 3px rgba(2,26,61,.08)` / `0 4px 12px rgba(2,26,61,.10)`.
- Focus visible : `0 0 0 3px rgba(2,26,61,.20)`.

## Ton visuel voulu
Corporate-tech **raffiné et sobre** (SaaS pro sénégalais), pas maximaliste.
Élégance par : espacement généreux, hiérarchie nette, alignements précis,
Palatino pour les titres qui donne une touche premium.
Responsive mobile-first. Accessible (RGAA). Icônes : Lucide (léger, cohérent).
