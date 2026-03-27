## Over dit project

De **Dakoppervlakte Calculator** is een desktop applicatie die het plannen van zonnepanelen vereenvoudigt. Door simpelweg een adres in te voeren, berekent de tool:

- **Exacte dakoppervlakte** van het gebouw
- **Optimale paneel-indeling** op basis van beschikbare ruimte
- **PV oriëntatie** van panelen (0-360°)
- **No-go zones** voor obstructies (schoorsteen, antennes, etc.)

De tool maakt gebruik van **OpenStreetMap data** en geometrie-berekeningen voor nauwkeurige resultaten.

---

## Snelstart

### Vereisten

- Python 3.8+
- Tkinter (meestal inbegrepen met Python)
- Internet (voor OpenStreetMap data)

### Installatie

```bash
# Clone het project
git clone https://github.com/Bremen-Bouwadviseurs/Dakoppervlakte-PV_berekeningen.git
cd Dakoppervlakte-PV_berekeningen

# Installeer dependencies
pip install -r requirements.txt

# Start de applicatie
python main.py
```

### Dependencies

```
Pillow >= 9.0.0
requests >= 2.28.0
```

---

## Hoe te gebruiken

1. **Voer een adres in**  
   Bijv: "Parallelweg 2-B, Heerlen"

2. **Stel paneelafmetingen in**  
   - Paneellengte (m)
   - Paneelbreedte (m)

3. **Kies oriëntatie**  
   Standaard 180° (zuid), naar wens aanpasbaar

4. **Klik "Bereken"**  
   De tool haalt gebouwdata op en berekent de paneel-indeling

5. **Verfijn resultaten** (optioneel)
   - Teken no-go zones in rood
   - Pas oriëntatie aan via dakrand selectie
   - Gebruik zoom en panorama voor details

---

## Technische Details

### Gebruikte APIs

- **Nominatim/Photon**: Adres-coördinaten conversie
- **Overpass**: OpenStreetMap gebouwdata
- **PIL**: Afbeelding verwerking en rendering

### Data Processing

- Projectie van lat/lon naar lokale meters (WGS84)
- Haversine-afstandsberekening
- Ray-casting algoritme voor polygon-tests
- Rotatie-invariante paneel-plaatsing

---

## Troubleshooting

| Probleem | Oorzaak | Oplossing |
|----------|--------|----------|
| "Adres niet gevonden" | Onvolledig adres | Voeg postcode toe (bijv. "1234 AB, Plaats") |
| "Geen gebouwcontour gevonden" | Adres in onbekend gebied | Probeer een ander adres dichterbij |
| Server timeout (429/502/503) | Overpass druk | Wacht 30 sec en probeer opnieuw |
| Tkinter import error | Tkinter niet geïnstalleerd | `apt install python3-tk` (Linux) |

---

## Licentie

Dit project is eigendom van Bremen Bouwadviseurs BV

Deze applicatie maakt gebruik van data van OpenStreetMap.
© OpenStreetMap contributors
Data beschikbaar onder de Open Database License (ODbL): https://opendatacommons.org/licenses/odbl/

Gebruikers van deze software dienen zelf te zorgen voor correcte naleving van de ODbL-voorwaarden indien de data verder wordt verwerkt of gedeeld.

---

## Versiegeschiedenis

- **v1.0.0** - Initiële release
  - Dakoppervlakte berekening
  - Paneel-indeling optimisatie
  - No-go zones ondersteuning
  - Interactive viewer

---

## Contact

Voor vragen of ondersteuning: info@bremen-bouwadviseurs.nl

