import copy
import random
from typing import Dict, List, Optional, Tuple

from katrain.core.lang import i18n
from katrain.core.sgf_parser import Move, SGFNode
from katrain.core.utils import evaluation_class, var_to_grid
from katrain.gui.style import INFO_PV_COLOR


class GameNode(SGFNode):
    """Represents a single game node, with one or more moves and placements."""

    def __init__(self, parent=None, properties=None, move=None):
        super().__init__(parent=parent, properties=properties, move=move)
        self.analysis = {"moves": {}, "root": None}
        self.ownership = None
        self.policy = None
        self.auto_undo = None  # None = not analyzed. False: not undone (good move). True: undone (bad move)
        self.ai_thoughts = ""
        self.note = ""
        self.move_number = 0
        self.time_used = 0
        self.analysis_visits_requested = 0
        self.undo_threshold = random.random()  # for fractional undos
        self.end_state = None

    def sgf_properties(self, save_comments_player=None, save_comments_class=None, eval_thresholds=None):
        properties = copy.copy(super().sgf_properties())
        note = self.note.strip()
        if self.points_lost and save_comments_class is not None and eval_thresholds is not None:
            show_class = save_comments_class[evaluation_class(self.points_lost, eval_thresholds)]
        else:
            show_class = False
        if (
            self.parent
            and self.parent.analysis_ready
            and self.analysis_ready
            and (note or ((save_comments_player or {}).get(self.player, False) and show_class))
        ):
            candidate_moves = self.parent.candidate_moves
            top_x = Move.from_gtp(candidate_moves[0]["move"]).sgf(self.board_size)
            best_sq = [
                Move.from_gtp(d["move"]).sgf(self.board_size) for d in candidate_moves[1:] if d["pointsLost"] <= 0.5
            ]
            if best_sq and "SQ" not in properties:
                properties["SQ"] = best_sq
            if top_x and "MA" not in properties:
                properties["MA"] = [top_x]
            comment = self.comment(sgf=True, interactive=False)
            if comment:
                properties["C"] = ["\n".join(properties.get("C", "")) + comment]
        if self.is_root:
            properties["C"] = [
                i18n._("SGF start message")
                + "\n"
                + "\n".join(properties.get("C", ""))
                + "\nSGF with review generated by KaTrain."
            ]
        if note:
            properties["C"] = ["\n".join(properties.get("C", "")) + f"\nNote: {self.note}"]
        return properties

    @staticmethod
    def order_children(children):
        return sorted(
            children, key=lambda c: 0.5 if c.auto_undo is None else int(c.auto_undo)
        )  # analyzed/not undone main, non-teach second, undone last

    # various analysis functions
    def analyze(
        self,
        engine,
        priority=0,
        visits=None,
        time_limit=True,
        refine_move=None,
        analyze_fast=False,
        find_alternatives=False,
    ):
        engine.request_analysis(
            self,
            lambda result: self.set_analysis(result, refine_move, find_alternatives),
            priority=priority,
            visits=visits,
            analyze_fast=analyze_fast,
            time_limit=time_limit,
            next_move=refine_move,
            find_alternatives=find_alternatives,
        )

    def update_move_analysis(self, move_analysis, move_gtp):
        cur = self.analysis["moves"].get(move_gtp)
        if cur is None:
            self.analysis["moves"][move_gtp] = {
                "move": move_gtp,
                "order": 999,
                **move_analysis,
            }  # some default values for keys missing in rootInfo
        else:
            cur["order"] = min(cur["order"], move_analysis.get("order", 999))  # parent arriving after child
            if cur["visits"] < move_analysis["visits"]:
                cur.update(move_analysis)

    def set_analysis(self, analysis_json: Dict, refine_move: Optional[Move], alternatives_mode: bool):
        if refine_move:
            pvtail = analysis_json["moveInfos"][0]["pv"] if analysis_json["moveInfos"] else []
            self.update_move_analysis(
                {"pv": [refine_move.gtp()] + pvtail, **analysis_json["rootInfo"]}, refine_move.gtp()
            )
        else:
            if alternatives_mode:
                for m in analysis_json["moveInfos"]:
                    m["order"] += 10  # offset for not making this top
            if refine_move is None and not alternatives_mode:
                for move_dict in self.analysis["moves"].values():
                    move_dict["order"] = 999  # old moves to end
            for move_analysis in analysis_json["moveInfos"]:
                self.update_move_analysis(move_analysis, move_analysis["move"])
            self.ownership = analysis_json.get("ownership")
            self.policy = analysis_json.get("policy")
            if not alternatives_mode:
                self.analysis["root"] = analysis_json["rootInfo"]
            if self.parent and self.move:
                analysis_json["rootInfo"]["pv"] = [self.move.gtp()] + (
                    analysis_json["moveInfos"][0]["pv"] if analysis_json["moveInfos"] else []
                )
                self.parent.update_move_analysis(
                    analysis_json["rootInfo"], self.move.gtp()
                )  # update analysis in parent for consistency

    @property
    def analysis_ready(self):
        return self.analysis["root"] is not None

    @property
    def score(self) -> Optional[float]:
        if self.analysis_ready:
            return self.analysis["root"].get("scoreLead")

    def format_score(self, score=None):
        score = score or self.score
        if score is not None:
            return f"{'B' if score >= 0 else 'W'}+{abs(score):.1f}"

    @property
    def winrate(self) -> Optional[float]:
        if self.analysis_ready:
            return self.analysis["root"].get("winrate")

    def format_winrate(self, win_rate=None):
        win_rate = win_rate or self.winrate
        if win_rate is not None:
            return f"{'B' if win_rate > 0.5 else 'W'} {max(win_rate,1-win_rate):.1%}"

    def move_policy_stats(self) -> Tuple[Optional[int], float, List]:
        single_move = self.move
        if single_move and self.parent:
            policy_ranking = self.parent.policy_ranking
            for ix, (p, m) in enumerate(policy_ranking):
                if m == single_move:
                    return ix + 1, p, policy_ranking
        return None, 0.0, []

    def make_pv(self, player, pv, interactive):
        pvtext = f"{player}{' '.join(pv)}"
        if interactive:
            pvtext = f"[u][ref={pvtext}][color={INFO_PV_COLOR}]{pvtext}[/color][/ref][/u]"
        return pvtext

    def comment(self, sgf=False, teach=False, details=False, interactive=True):
        single_move = self.move
        if not self.parent or not single_move:  # root
            if self.root:
                return f"{i18n._('komi')}: {self.komi:.1f}\n{i18n._('ruleset')}: {i18n._(self.get_property('RU','Japanese').lower())}\n"
            return ""

        text = i18n._("move").format(number=self.depth) + f": {single_move.player} {single_move.gtp()}\n"
        if self.analysis_ready:
            score = self.score
            if sgf:
                text += i18n._("Info:score").format(score=self.format_score(score)) + "\n"
                text += i18n._("Info:winrate").format(winrate=self.format_winrate()) + "\n"
            if self.parent and self.parent.analysis_ready:
                previous_top_move = self.parent.candidate_moves[0]
                if sgf or details:
                    if previous_top_move["move"] != single_move.gtp():
                        points_lost = self.points_lost
                        if sgf and points_lost > 0.5:
                            text += i18n._("Info:point loss").format(points_lost=points_lost) + "\n"
                        top_move = previous_top_move["move"]
                        score = self.format_score(previous_top_move["scoreLead"])
                        text += i18n._("Info:top move").format(top_move=top_move, score=score,) + "\n"
                    else:
                        text += i18n._("Info:best move") + "\n"
                    if previous_top_move.get("pv") and (sgf or details):
                        pv = self.make_pv(single_move.player, previous_top_move["pv"], interactive)
                        text += i18n._("Info:PV").format(pv=pv) + "\n"

                if sgf or details or teach:
                    currmove_pol_rank, currmove_pol_prob, policy_ranking = self.move_policy_stats()
                    if currmove_pol_rank is not None:
                        policy_rank_msg = i18n._("Info:policy rank")
                        text += policy_rank_msg.format(rank=currmove_pol_rank, probability=currmove_pol_prob) + "\n"
                    if currmove_pol_rank is None or currmove_pol_rank != 1 and (sgf or details):
                        policy_best_msg = i18n._("Info:policy best")
                        pol_move, pol_prob = policy_ranking[0][1].gtp(), policy_ranking[0][0]
                        text += policy_best_msg.format(move=pol_move, probability=pol_prob) + "\n"
            if self.auto_undo and sgf:
                text += i18n._("Info:teaching undo") + "\n"
                top_pv = self.analysis_ready and self.candidate_moves[0].get("pv")
                if top_pv:
                    text += i18n._("Info:undo predicted PV").format(pv=f"{self.next_player}{' '.join(top_pv)}") + "\n"
            if self.ai_thoughts and (sgf or details):
                text += "\n" + i18n._("Info:AI thoughts").format(thoughts=self.ai_thoughts)
        else:
            text = i18n._("No analysis available") if sgf else i18n._("Analyzing move...")

        if "C" in self.properties:
            text += "\n[u]SGF Comments:[/u]\n" + "\n".join(self.properties["C"])

        return text

    @property
    def points_lost(self) -> Optional[float]:
        single_move = self.move
        if single_move and self.parent and self.analysis_ready and self.parent.analysis_ready:
            parent_score = self.parent.score
            score = self.score
            return self.player_sign(single_move.player) * (parent_score - score)

    @property
    def parent_realized_points_lost(self) -> Optional[float]:
        single_move = self.move
        if (
            single_move
            and self.parent
            and self.parent.parent
            and self.analysis_ready
            and self.parent.parent.analysis_ready
        ):
            parent_parent_score = self.parent.parent.score
            score = self.score
            return self.player_sign(single_move.player) * (score - parent_parent_score)

    @staticmethod
    def player_sign(player):
        return {"B": 1, "W": -1, None: 0}[player]

    @property
    def candidate_moves(self) -> List[Dict]:
        if not self.analysis_ready:
            return []
        if not self.analysis["moves"]:
            polmoves = self.policy_ranking
            top_polmove = polmoves[0][1] if polmoves else Move(None)  # if no info at all, pass
            return [
                {
                    **self.analysis["root"],
                    "pointsLost": 0,
                    "order": 0,
                    "move": top_polmove.gtp(),
                    "pv": [top_polmove.gtp()],
                }
            ]  # single visit -> go by policy/root

        root_score = self.analysis["root"]["scoreLead"]
        move_dicts = list(self.analysis["moves"].values())  # prevent incoming analysis from causing crash
        return sorted(
            [
                {"pointsLost": self.player_sign(self.next_player) * (root_score - d["scoreLead"]), **d}
                for d in move_dicts
            ],
            key=lambda d: (d["order"], d["pointsLost"]),
        )

    @property
    def policy_ranking(self) -> Optional[List[Tuple[float, Move]]]:  # return moves from highest policy value to lowest
        if self.policy:
            szx, szy = self.board_size
            policy_grid = var_to_grid(self.policy, size=(szx, szy))
            moves = [(policy_grid[y][x], Move((x, y), player=self.next_player)) for x in range(szx) for y in range(szy)]
            moves.append((self.policy[-1], Move(None, player=self.next_player)))
            return sorted(moves, key=lambda mp: -mp[0])
