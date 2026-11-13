"""Notebook charts carry each line's last value at its right end, as the web dashboard does."""

import unittest

import pandas as pd
import plotly.graph_objects as go

from desk import charts
from desk.charts import end_labels


class EndLabelTests(unittest.TestCase):
    def setUp(self):
        self.days = pd.date_range("2026-11-01", periods=3, freq="D")

    def texts(self, fig):
        return [(note.text, note.y) for note in fig.layout.annotations]

    def test_dollar_lines_show_their_last_value(self):
        fig = go.Figure(go.Scatter(x=self.days, y=[10.0, 20.0, -5.5], mode="lines"))
        fig.update_layout(yaxis_tickformat="$,.1f")
        self.assertEqual(self.texts(end_labels(fig)), [("-$5.50", -5.5)])
        self.assertGreaterEqual(fig.layout.margin.r, 80)

    def test_stacked_layers_sit_on_the_stack_and_show_their_own_value(self):
        fig = go.Figure([go.Scatter(x=self.days, y=[1, 2, 3], stackgroup="v", name="A"),
                         go.Scatter(x=self.days, y=[4, 5, 6], stackgroup="v", name="B")])
        fig.update_layout(yaxis_tickformat="$,.1f")
        self.assertEqual(self.texts(end_labels(fig)), [("$3.00", 3.0), ("$6.00", 9.0)])

    def test_index_and_percent_axes_and_skipped_traces(self):
        fig = go.Figure([go.Scatter(x=self.days, y=[100, 101, 104.26], name="Desk"),
                         go.Scatter(x=[self.days[-1]], y=[104.3], mode="markers", name="live"),     # a marker: no label
                         go.Scatter(x=[1.0, 2.0], y=[0.1, 0.2], mode="lines", name="regression")])  # not dates: no label
        fig.update_layout(yaxis_title="Index (start = 100)")
        self.assertEqual(self.texts(end_labels(fig)), [("104.3", 104.26)])
        apr = go.Figure(go.Scatter(x=self.days, y=[5.0, float("nan"), 7.25]))
        apr.update_layout(yaxis_ticksuffix="%")
        self.assertEqual(self.texts(end_labels(apr)), [("7.25%", 7.25)])   # the last finite value

    def test_percent_tickformat_scales_fractions(self):
        fig = go.Figure(go.Scatter(x=self.days, y=[0.0, 0.05, -0.066]))
        fig.update_layout(yaxis_tickformat=".0%")
        self.assertEqual(self.texts(end_labels(fig)), [("-6.60%", -0.066)])

    def test_subplot_lines_label_on_their_own_axes(self):
        from plotly.subplots import make_subplots

        fig = make_subplots(rows=2, cols=1)
        fig.add_trace(go.Scatter(x=self.days, y=[1, 2, 3]), row=2, col=1)
        note = end_labels(fig).layout.annotations[-1]
        self.assertEqual((note.xref, note.yref), ("x2", "y2"))


if __name__ == "__main__":
    unittest.main()


class CumulativePnlColourTests(unittest.TestCase):
    def test_the_line_splits_at_zero_and_both_parts_meet_there(self):
        from desk.hl.account_analytics import split_at_zero

        days = pd.date_range("2026-11-01", periods=4, freq="D")
        above, below = split_at_zero(pd.Series([10.0, -10.0, -5.0, 5.0], index=days))
        crossing = days[0] + (days[1] - days[0]) / 2                  # 10 -> -10 crosses zero half way
        self.assertEqual(above[crossing], 0.0)
        self.assertEqual(below[crossing], 0.0)
        self.assertTrue(pd.isna(above[days[1]]) and below[days[1]] == -10.0)
        self.assertTrue(above[days[3]] == 5.0 and pd.isna(below[days[3]]))


class SplitLineLabelTests(unittest.TestCase):
    def test_a_line_drawn_as_green_and_red_parts_gets_one_label_at_its_real_end(self):
        days = pd.date_range("2026-11-01", periods=3, freq="D")
        fig = go.Figure([go.Scatter(x=days, y=[5.0, None, None], legendgroup="pnl", line={"color": "green"}),
                         go.Scatter(x=days, y=[None, -3.0, -4.5], legendgroup="pnl", line={"color": "red"})])
        fig.update_layout(yaxis_tickformat="$,.1f")
        end_labels(fig)
        self.assertEqual([(note.text, note.font.color) for note in fig.layout.annotations], [("-$4.50", "red")])


class SpreadTests(unittest.TestCase):
    def test_labels_closer_than_a_line_are_pushed_apart_in_order(self):
        import plotly.graph_objects as go

        days = pd.date_range("2026-01-01", periods=30, freq="D")
        fig = go.Figure([go.Scatter(x=days, y=[1000.0] * 30, name="IB", stackgroup="v"),
                         go.Scatter(x=days, y=[5.0] * 30, name="HL", stackgroup="v"),
                         go.Scatter(x=days, y=[0.0] * 29 + [3000.0], name="far")])
        charts.end_labels(fig)
        notes = {note.text: note for note in fig.layout.annotations}
        ib, total = notes["1,000.00"], notes["5.00"]   # HL's own value, labelled at the stack top (1,005)
        self.assertGreaterEqual((total.yshift or 0) - (ib.yshift or 0), charts.END_LABEL_GAP_PX - 1)   # the upper one stays above
        self.assertFalse(notes["3,000.00"].yshift)                                                     # a lone label is not moved
