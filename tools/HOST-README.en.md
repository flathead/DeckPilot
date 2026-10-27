# DeckPilot Host 0.3.0

[Русский](HOST-README.md) | **English**

Extract this bundle on the PC you want to control from the Deck.

**Linux:** open Setup-Linux.desktop and allow it to run if your file manager asks. You can also run `bash setup-linux.sh` from a terminal in this directory. Setup asks for sudo access.

**Windows 10/11:** open Setup-Windows.cmd and approve the administrator prompt. Use the Private network profile for pairing.

On the Deck, install deckpilot-0.3.0.zip through Decky Loader. Open DeckPilot and connect the PC. The plugin starts in Russian; choose English from the Язык / Language list if you prefer. PC wizards use Russian.

Keep the PC wizard open. Enter its eight-digit PIN on the Deck. Compare the verification codes on both devices. If they match, type YES on the PC within one minute. Setup handles the keys.

Steam Headless is optional on Linux. If it is not installed, choose PC power controls. To add it later, rerun PC setup with an integration and enable its controls in DeckPilot settings.

Read [README.en.md](https://github.com/flathead/DeckPilot/blob/v0.3.0/README.en.md) for details, system changes and test limits. This release still needs testing on a physical Steam Deck and Windows PC.
