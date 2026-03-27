"""Tests for src/charts.py — chart extraction, rendering, and stripping."""

from __future__ import annotations

from charts import extract_charts, render_chart, strip_charts


class TestExtractCharts:
    def test_no_charts(self) -> None:
        assert extract_charts("Just a normal report with no charts.") == []

    def test_single_chart(self) -> None:
        response = (
            'Some text\n<chart title="HRV Trend" section="Key Metrics">\n'
            "fig, ax = plt.subplots()\n"
            "</chart>\nMore text"
        )
        blocks = extract_charts(response)
        assert len(blocks) == 1
        assert blocks[0].title == "HRV Trend"
        assert blocks[0].section == "Key Metrics"
        assert "fig, ax = plt.subplots()" in blocks[0].code

    def test_multiple_charts(self) -> None:
        response = (
            '<chart title="A" section="S1">\ncode_a\n</chart>\n'
            '<chart title="B" section="S2">\ncode_b\n</chart>\n'
            '<chart title="C" section="S3">\ncode_c\n</chart>'
        )
        blocks = extract_charts(response)
        assert len(blocks) == 3
        assert [b.title for b in blocks] == ["A", "B", "C"]

    def test_empty_code_skipped(self) -> None:
        response = '<chart title="Empty" section="S">\n\n</chart>'
        assert extract_charts(response) == []

    def test_no_section_attribute(self) -> None:
        response = '<chart title="Solo">\nfig, ax = plt.subplots()\n</chart>'
        blocks = extract_charts(response)
        assert len(blocks) == 1
        assert blocks[0].section == ""

    def test_multiline_code(self) -> None:
        code = "import plotly.graph_objects as go\nfig = go.Figure()\nfig.add_trace(go.Scatter(y=[1,2,3]))"
        response = f'<chart title="T" section="S">\n{code}\n</chart>'
        blocks = extract_charts(response)
        assert len(blocks) == 1
        assert "fig.add_trace" in blocks[0].code


class TestStripCharts:
    def test_no_charts(self) -> None:
        text = "Just text"
        assert strip_charts(text) == "Just text"

    def test_single_chart_removed(self) -> None:
        text = 'Before\n<chart title="X" section="Y">\ncode\n</chart>\nAfter'
        result = strip_charts(text)
        assert "Before" in result
        assert "After" in result
        assert "<chart" not in result
        assert "code" not in result

    def test_multiple_charts_removed(self) -> None:
        text = (
            "A\n"
            '<chart title="1" section="S">\nc1\n</chart>\n'
            "B\n"
            '<chart title="2" section="S">\nc2\n</chart>\n'
            "C"
        )
        result = strip_charts(text)
        assert "<chart" not in result
        assert "A" in result
        assert "B" in result
        assert "C" in result

    def test_surrounding_content_preserved(self) -> None:
        text = (
            "## Key Metrics\n\n"
            "HRV is trending down.\n\n"
            '<chart title="HRV" section="Key Metrics">\nfig = ...\n</chart>\n\n'
            "## Recovery Status\n\nReady to push."
        )
        result = strip_charts(text)
        assert "## Key Metrics" in result
        assert "HRV is trending down." in result
        assert "## Recovery Status" in result
        assert "Ready to push." in result


class TestRenderChart:
    def test_valid_simple_chart(self) -> None:
        code = (
            "import plotly.graph_objects as go\n"
            "fig = go.Figure(go.Scatter(x=[1,2,3], y=[4,5,6], mode='lines+markers'))\n"
            "fig.update_layout(template='plotly_white')"
        )
        result = render_chart(code, {"current_week": {"days": []}})
        assert result is not None
        # PNG magic bytes.
        assert result[:4] == b"\x89PNG"

    def test_code_using_data(self) -> None:
        code = (
            "import plotly.graph_objects as go\n"
            "days = data['current_week']['days']\n"
            "values = [d['val'] for d in days]\n"
            "fig = go.Figure(go.Bar(x=list(range(len(values))), y=values))\n"
        )
        health_data = {"current_week": {"days": [{"val": 1}, {"val": 2}, {"val": 3}]}}
        result = render_chart(code, health_data)
        assert result is not None
        assert result[:4] == b"\x89PNG"

    def test_annotations(self) -> None:
        code = (
            "import plotly.graph_objects as go\n"
            "fig = go.Figure(go.Scatter(x=[1,2,3], y=[10,5,15], mode='lines+markers'))\n"
            "fig.add_hline(y=10, line_dash='dash', annotation_text='baseline')\n"
            "fig.add_annotation(x=2, y=5, text='Low point', arrowhead=2)\n"
            "fig.update_layout(template='plotly_white')"
        )
        result = render_chart(code, {})
        assert result is not None
        assert result[:4] == b"\x89PNG"

    def test_missing_fig_returns_none(self) -> None:
        code = "x = 42"
        result = render_chart(code, {})
        assert result is None

    def test_syntax_error_returns_none(self) -> None:
        code = "def bad(\n"
        result = render_chart(code, {})
        assert result is None

    def test_runtime_error_returns_none(self) -> None:
        code = "1 / 0"
        result = render_chart(code, {})
        assert result is None

    def test_restricted_open_blocked(self) -> None:
        code = "fig = open('/etc/passwd')"
        result = render_chart(code, {})
        assert result is None

    def test_restricted_eval_blocked(self) -> None:
        code = "fig = eval('1+1')"
        result = render_chart(code, {})
        assert result is None
