"""Support-Bundle entfernt Konsolenausgaben und kürzt Befundpfade (Art. 5 DSGVO)."""

from app.routes.api_maintenance import minimize_job_summaries


def test_support_bundle_minimizes_bootdrill_and_bitrot_summaries():
    jobs = [
        {"id": 1, "summary": {"serial_tail": "host backup01 login:"}},
        {
            "id": 2,
            "summary": {
                "pairs": [
                    {
                        "name": "Fotos",
                        "mismatches": [{"path": "privat/2024/urlaub.jpg", "size": 1}],
                    }
                ]
            },
        },
        {"id": 3, "summary": None},
    ]
    out = minimize_job_summaries(jobs)
    assert out[0]["summary"]["serial_tail"] == "<entfernt>"
    assert out[1]["summary"]["pairs"][0]["mismatches"][0] == {
        "path": "urlaub.jpg",
        "size": 1,
    }
    assert out[2] == jobs[2]
    assert jobs[1]["summary"]["pairs"][0]["mismatches"][0]["path"].startswith("privat")
