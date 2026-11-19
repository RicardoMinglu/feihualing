from scripts.build_curriculum_review_html import create_payload


def test_curriculum_review_uses_report_and_preserves_candidate_text():
    review = create_payload()
    assert len(review['ambiguous']) == 5
    assert len(review['missing']) == 46
    assert len({item['key'] for item in review['ambiguous']}) == 5
    furong = next(item for item in review['ambiguous'] if item['title'] == '芙蓉楼送辛渐')
    assert len(furong['variants']) == 3
    assert furong['variants'][-1]['id'].startswith('reference:')
    assert furong['variants'][-1]['paragraphs'][0].startswith('寒雨连江夜入吴')
    assert all(variant['paragraphs'] and variant['diff_html'] for variant in furong['variants'])
