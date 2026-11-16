from scripts.evaluate_application import distractor_citations_excluded


def test_exclusion_and_its_citation_share_a_paragraph():
    assert distractor_citations_excluded(
        ["历史记录只描述故障数量，不提供当前步骤，因此不能作为处理依据。引用：排障（历史背景）。"],
        ["（历史背景）"])
    assert not distractor_citations_excluded(
        ["按排障（历史背景）重启服务即可。"], ["（历史背景）"])
    assert not distractor_citations_excluded(
        ["别的文档不提供处理步骤。\n\n按排障（历史背景）重启服务即可。"], ["（历史背景）"])
