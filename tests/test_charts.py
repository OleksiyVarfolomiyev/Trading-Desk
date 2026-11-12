import unittest

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from desk.charts import dollar_axes, style, wrap_title


class ChartStyleTests(unittest.TestCase):
    def test_long_titles_wrap_to_the_figure_width_and_keep_breaks(self):
        title = "Current IB holdings over the past year (hypothetical: today's positions held throughout)"
        wrapped = wrap_title(title, 600)
        self.assertGreater(wrapped.count("<br>"), 0)
        self.assertEqual(wrapped.replace("<br>", " "), title)
        self.assertEqual(wrap_title("Short", 600), "Short")
        self.assertEqual(wrap_title("A<br>B", 600), "A<br>B")

    def test_legend_goes_above_the_plot_below_the_title(self):
        fig = go.Figure([go.Scatter(y=[1, 2], name="Book"), go.Scatter(y=[2, 1], name="SPY")])
        fig.update_layout(title="A title", legend={"orientation": "v", "x": 1.02}, height=400)
        style(fig)
        self.assertEqual((fig.layout.legend.orientation, fig.layout.legend.yref), ("h", "container"))
        self.assertLess(fig.layout.legend.y, fig.layout.title.y)  # legend sits under the title
        self.assertGreaterEqual(fig.layout.margin.t, 60)

    def test_single_trace_reserves_no_legend_row(self):
        fig = go.Figure(go.Scatter(y=[1, 2], name="Account value"))
        fig.update_layout(title="IB account value", height=400)
        style(fig)
        self.assertIsNone(fig.layout.legend.orientation)

    def test_dollar_axes_move_the_unit_into_the_ticks(self):
        fig = make_subplots(rows=3, cols=1)
        fig.update_yaxes(title_text="USD", row=1, col=1)
        fig.update_yaxes(title_text="Daily realized PnL ($)", row=2, col=1)
        fig.update_yaxes(title_text="Weeks", row=3, col=1)
        dollar_axes(fig)
        self.assertIsNone(fig.layout.yaxis.title.text)
        self.assertEqual((fig.layout.yaxis.tickformat, fig.layout.yaxis.tickprefix), ("$,.1f", ""))
        self.assertEqual(fig.layout.yaxis2.title.text, "Daily realized PnL")
        self.assertEqual(fig.layout.yaxis2.tickformat, "$,.1f")
        self.assertIsNone(fig.layout.yaxis3.tickformat)  # not a dollar axis


if __name__ == "__main__":
    unittest.main()


class WholeDollarAxisTests(unittest.TestCase):
    def test_an_axis_that_chose_whole_dollars_keeps_them(self):
        import plotly.graph_objects as go

        fig = go.Figure(go.Scatter(x=[1, 2], y=[1, 2]))
        fig.update_xaxes(title_text="Volume ($)", tickformat="$,.0f")
        fig.update_yaxes(title_text="PnL ($)")
        dollar_axes(fig)
        self.assertEqual((fig.layout.xaxis.tickformat, fig.layout.yaxis.tickformat), ("$,.0f", "$,.1f"))
