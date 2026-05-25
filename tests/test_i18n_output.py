import unittest

from describe_videos import build_content, transcript_only_text


def _text_blocks(content):
    return '\n'.join(
        block['text'] for block in content
        if block.get('type') == 'text'
    )


class OutputLanguageTests(unittest.TestCase):
    def test_build_content_does_not_force_polish_when_en_prompt_selected(self):
        content = build_content(
            frames=[],
            filename='clip.mp4',
            people='Filip, Jadzia',
            context='motorcycle trip',
            transcript='[00:01] hello',
            output_language='en',
        )
        text = _text_blocks(content)

        self.assertIn('Based on the frames above and the speech transcript', text)
        self.assertIn('Critical formatting rules', text)
        self.assertNotIn('Pisz po ' + 'polsku', text)
        self.assertNotIn('Transkrypcja mowy', text)

    def test_build_content_uses_polish_instructions_for_pl_output(self):
        content = build_content(
            frames=[],
            filename='clip.mp4',
            people='Filip, Jadzia',
            context='wyprawa motocyklowa',
            transcript='[00:01] cześć',
            output_language='pl',
        )
        text = _text_blocks(content)

        self.assertIn('Na podstawie powyższych klatek i transkrypcji mowy', text)
        self.assertIn('Krytyczne zasady formatowania', text)
        self.assertNotIn('Pisz po ' + 'polsku', text)

    def test_transcript_only_heading_respects_output_language_pl(self):
        text = transcript_only_text(
            'clip.mp4',
            '[00:01] Cześć',
            output_language='pl',
        )

        self.assertTrue(text.startswith('clip.mp4 - transkrypcja mowy'))
        self.assertIn('[00:01] Cześć', text)

    def test_transcript_only_heading_respects_output_language_en(self):
        text = transcript_only_text(
            'clip.mp4',
            '[00:01] Hello',
            output_language='en',
        )

        self.assertTrue(text.startswith('clip.mp4 - speech transcript'))
        self.assertIn('[00:01] Hello', text)


if __name__ == '__main__':
    unittest.main()
