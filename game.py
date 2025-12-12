# game.py
from __future__ import annotations
import random
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional


SUITS = ["C", "D", "H", "S"]  # Clubs, Diamonds, Hearts, Spades
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]


def create_deck() -> List[str]:
    """Return a fresh shuffled 52-card deck (no jokers)."""
    deck = [f"{rank}{suit}" for suit in SUITS for rank in RANKS]
    random.shuffle(deck)
    return deck


class Stage(Enum):
    WAITING_FOR_PLAYERS = auto()
    PREFLOP = auto()
    FLOP = auto()
    TURN = auto()
    RIVER = auto()
    SHOWDOWN = auto()


@dataclass
class Player:
    user_id: int
    name: str
    avatar_file_id: Optional[str] = None
    chips: int = 1000
    current_bet: int = 0
    folded: bool = False
    hole_cards: List[str] = field(default_factory=list)

    # optionale Felder, die vom Bot-Code genutzt werden (AFK, etc.)
    last_action_time: Optional[float] = None
    afk_warned: bool = False

    def reset_for_new_hand(self):
        self.current_bet = 0
        self.folded = False
        self.hole_cards.clear()


@dataclass
class Table:
    chat_id: int
    thread_id: Optional[int]
    host_id: Optional[int] = None

    small_blind: int = 10
    big_blind: int = 20
    starting_chips: int = 1000

    max_hands: int = 0   # 0 = unlimited / free play
    hands_played: int = 0

    max_players: int = 0   # 0 = unlimited seats

    players: Dict[int, Player] = field(default_factory=dict)
    deck: List[str] = field(default_factory=list)
    community_cards: List[str] = field(default_factory=list)
    pot: int = 0
    stage: Stage = Stage.WAITING_FOR_PLAYERS
    dealer_index: int = 0
    turn_order: List[int] = field(default_factory=list)
    current_turn_idx: int = 0
    current_bet: int = 0

    # =====================================================
    #                  PLAYER MANAGEMENT
    # =====================================================

    def add_player(
        self,
        user_id: int,
        name: str,
        avatar_file_id: Optional[str] = None,
    ):
        """Fügt einen neuen (menschlichen) Spieler mit Starting-Stack hinzu."""
        if user_id not in self.players:
            self.players[user_id] = Player(
                user_id=user_id,
                name=name,
                avatar_file_id=avatar_file_id,
                chips=self.starting_chips,
            )

    def remove_player(self, user_id: int):
        if user_id in self.players:
            del self.players[user_id]

    def active_players(self) -> List[Player]:
        """
        Aktive Spieler = alle, die NICHT gefoldet haben.
        Auch Spieler mit 0 Chips (ALL-IN) bleiben aktiv für das Showdown.
        """
        return [p for p in self.players.values() if not p.folded]

    # =====================================================
    #                  HAND / ROUND SETUP
    # =====================================================

    def reset_for_new_hand(self):
        """
        Bereitet eine neue Hand vor:
        - neues Deck
        - Pot & Einsätze auf 0
        - Spieler-Status für die Hand zurücksetzen
        - Turn-Order neu mischen
        - Stage auf PREFLOP
        """
        self.deck = create_deck()
        self.community_cards.clear()
        self.pot = 0
        self.current_bet = 0

        for p in self.players.values():
            p.reset_for_new_hand()

        # Alle Spieler zufällig mischen (keine Bots mehr)
        if self.players:
            ids = list(self.players.keys())
            random.shuffle(ids)
            self.turn_order = ids
            self.current_turn_idx = 0

        self.stage = Stage.PREFLOP

    def deal_hole_cards(self):
        """Gibt jedem Spieler zwei Hole Cards."""
        for player in self.players.values():
            player.hole_cards = [self.deck.pop(), self.deck.pop()]

    def deal_flop(self):
        """Burn + 3 Community Cards."""
        self.deck.pop()
        self.community_cards.extend([self.deck.pop(), self.deck.pop(), self.deck.pop()])
        self.stage = Stage.FLOP
        self.current_bet = 0
        for p in self.players.values():
            p.current_bet = 0

    def deal_turn(self):
        """Burn + 1 Community Card (Turn)."""
        self.deck.pop()
        self.community_cards.append(self.deck.pop())
        self.stage = Stage.TURN
        self.current_bet = 0
        for p in self.players.values():
            p.current_bet = 0

    def deal_river(self):
        """Burn + 1 Community Card (River)."""
        self.deck.pop()
        self.community_cards.append(self.deck.pop())
        self.stage = Stage.RIVER
        self.current_bet = 0
        for p in self.players.values():
            p.current_bet = 0

    # =====================================================
    #                    TURN ORDER
    # =====================================================

    def next_turn(self) -> Optional[int]:
        """
        Advance to next player that:
        - nicht gefoldet hat
        - noch Chips > 0 hat (sonst ist er effectively ALL-IN und hat keine Aktionen mehr)
        """
        if not self.turn_order:
            return None

        for _ in range(len(self.turn_order)):
            self.current_turn_idx = (self.current_turn_idx + 1) % len(self.turn_order)
            uid = self.turn_order[self.current_turn_idx]
            p = self.players.get(uid)
            # Spieler mit 0 Chips sind ALL-IN -> keine Action mehr, aber bleiben im Showdown
            if p and not p.folded and p.chips > 0:
                return uid
        return None

    def current_player_id(self) -> Optional[int]:
        """
        Liefert den aktuellen Spieler, der an der Reihe ist.

        Spieler mit 0 Chips (ALL-IN) werden übersprungen (dürfen nichts mehr machen),
        bleiben aber für das Showdown aktiv. Wenn alle entweder gefoldet oder all-in sind,
        gibt diese Funktion None zurück – dann kann die Logik im Bot automatisch
        Streets aufdecken / Showdown triggern.
        """
        if not self.turn_order:
            return None
        uid = self.turn_order[self.current_turn_idx]
        p = self.players.get(uid)
        if p and not p.folded and p.chips > 0:
            return uid
        return self.next_turn()

    # =====================================================
    #                 BETTING / POT LOGIC
    # =====================================================

    def fold(self, user_id: int):
        p = self.players[user_id]
        p.folded = True

    def check_or_call(self, user_id: int) -> int:
        """
        Check oder Call.

        Rückgabe: Betrag, der in dieser Action in den Pot geht.
        """
        p = self.players[user_id]
        to_call = self.current_bet - p.current_bet
        if to_call <= 0:
            # reiner Check
            return 0
        amount = min(to_call, p.chips)
        p.chips -= amount
        p.current_bet += amount
        self.pot += amount
        return amount

    def raise_bet(self, user_id: int, amount: int) -> int:
        """
        Raise um 'amount' *zusätzlich* zum Call-Betrag.

        Kein Side-Pot-System, aber ALL-IN wird sauber unterstützt:
        - Spieler zahlt maximal seine verbleibenden Chips
        - current_bet folgt dem höchsten Einsatz
        """
        p = self.players[user_id]
        to_call = self.current_bet - p.current_bet
        total_needed = to_call + amount
        total = min(total_needed, p.chips)
        p.chips -= total
        p.current_bet += total
        self.pot += total

        # Höchsten Einsatz übernehmen
        if p.current_bet > self.current_bet:
            self.current_bet = p.current_bet
        return total

    # =====================================================
    #            ROUND COMPLETION / STAGE ADVANCE
    # =====================================================

    def everyone_matched_or_folded(self) -> bool:
        """
        Prüft, ob die aktuelle Setzrunde abgeschlossen ist.

        Idee:
        - Gefoldete Spieler werden ignoriert.
        - Spieler mit 0 Chips (ALL-IN) werden für die Einsatz-Gleichheit ignoriert,
          d.h. sie dürfen "hinten liegen", blockieren die Runde aber nicht mehr.
        - Wenn es nur 0 oder 1 aktive Spieler gibt -> Runde de facto beendet.
        - Wenn es keine Spieler mit Chips > 0 gibt (alle all-in) -> Runde beendet.
        - Sonst müssen alle Spieler mit Chips > 0 denselben current_bet haben.
        """
        active = [p for p in self.players.values() if not p.folded]

        # 0 oder 1 aktiver Spieler -> Setzrunde/Hand de facto beendet
        if len(active) <= 1:
            return True

        # Spieler, die noch Chips haben (können theoretisch noch setzen)
        betting_players = [p for p in active if p.chips > 0]

        # Wenn niemand mehr Chips hat (alle all-in) -> Runde abgeschlossen
        if not betting_players:
            return True

        bets = {p.current_bet for p in betting_players}
        return len(bets) == 1

    def advance_stage_if_needed(self):
        """
        Advance to next stage once betting round is done.

        Nutzt die everyone_matched_or_folded()-Logik,
        damit ALL-IN-Situationen:
        - nicht hängen bleiben
        - aber die Hand nicht "zu früh" abgebrochen wird.
        """
        if not self.everyone_matched_or_folded():
            return

        # wenn nur ein Spieler übrig ist (alle anderen gefoldet)
        active = [p for p in self.players.values() if not p.folded]
        if len(active) == 1:
            # Sofortiger Gewinner – Showdown ohne weiteres Board.
            self.stage = Stage.SHOWDOWN
            return

        if self.stage == Stage.PREFLOP:
            self.deal_flop()
        elif self.stage == Stage.FLOP:
            self.deal_turn()
        elif self.stage == Stage.TURN:
            self.deal_river()
        elif self.stage == Stage.RIVER:
            self.stage = Stage.SHOWDOWN
