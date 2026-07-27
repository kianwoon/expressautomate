from app.services.ingest.preprocess import to_text


def test_html_tables_become_readable_lines():
    """Recruitment emails carry job details in tables constantly."""
    html = (
        "<table><tr><td>Salary</td><td>Up to $3500</td></tr>"
        "<tr><td>Location</td><td>Greenwich Drive</td></tr></table>"
    )

    result = to_text(html)

    assert "Salary" in result
    assert "Up to $3500" in result
    assert "Greenwich Drive" in result
    assert "<td>" not in result


def test_bullet_structure_survives():
    html = "<ul><li>Coordinate with finance</li><li>Prepare invoices</li></ul>"

    result = to_text(html)

    assert "Coordinate with finance" in result
    assert "Prepare invoices" in result
    assert result.count("\n") >= 1, "list items must not run together"


def test_subject_and_sender_are_prepended_as_context():
    result = to_text("<p>body</p>", subject="Finance officer", sender="e@x.com")

    assert result.startswith("SUBJECT: Finance officer")
    assert "SENDER: e@x.com" in result


def test_forwarded_content_is_kept():
    """Job orders arrive forwarded constantly; trimming them loses the job."""
    html = (
        "<p>FYI</p><div>From: client@example.com<br>"
        "We need a QA Executive, $3,700-$4,500</div>"
    )

    result = to_text(html)

    assert "QA Executive" in result
    assert "$3,700-$4,500" in result


def test_script_and_style_are_removed():
    html = "<style>.x{color:red}</style><script>alert(1)</script><p>Real content</p>"

    result = to_text(html)

    assert "Real content" in result
    assert "alert" not in result
    assert "color:red" not in result


def test_output_is_stable_for_offset_verification():
    """Evidence offsets index into this output, so it must be deterministic."""
    html = "<p>Up to $3500</p>"

    assert to_text(html) == to_text(html)
