Catalog file format
===================

One JSON file per album. Name it however you like.
The script matches albums by the "artist" and "album" fields (case-insensitive).

Example — single LP (2 sides):

  {
    "artist": "SCH",
    "album": "JVLIVS Prequel : Giulio",
    "sides": {
      "A": [
        {"title": "Track 1", "duration": 210},
        {"title": "Track 2", "duration": 185}
      ],
      "B": [
        {"title": "Garcimore", "duration": 172},
        {"title": "Track 4",   "duration": 198}
      ]
    }
  }

Example — double LP (4 sides):

  {
    "artist": "Pink Floyd",
    "album": "The Wall",
    "sides": {
      "A": [ ... ],
      "B": [ ... ],
      "C": [ ... ],
      "D": [ ... ]
    }
  }

Notes:
  - "duration" is in whole seconds
  - Track order within each side matters
  - Durations can be found on Discogs, the sleeve, or Spotify
  - The script auto-creates a file from MusicBrainz on first play;
    edit it if the data is wrong (digital editions often differ from vinyl)

Language:
  - Default UI language is English
  - Set VINYLGUARD_LANG=fr for French
