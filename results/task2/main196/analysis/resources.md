| item | value |
|---|---|
| feature caching: images | 11174 |
| feature caching: encode time (s) | 2216.8 |
| feature caching: peak GPU MB | 1766 |
| feature cache size (GB) | 3.13 |
| training total (h) | 1.57 |
| stage caption_alignment time (h) | 0.74 |
| stage visual_instruction time (h) | 0.55 |
| stage natural_vqa time (h) | 0.28 |
| trainable params, caption_alignment | 1,837,056 |
| trainable params, visual_instruction | 6,424,576 |
| trainable params, natural_vqa | 6,424,576 |
| training peak GPU MB (allocated) | 1611 |
| skipped (overflow) updates | 5 |
| GPU | Tesla T4 |
| gen s/answer grounding/correct | 0.0567 |
| gen s/answer grounding/swap | 0.0328 |
| gen s/answer grounding/mismatched | 0.0307 |
| gen s/answer grounding/no_image | 0.0236 |
| gen s/answer vqa/correct | 0.0373 |
| gen s/answer vqa/swap | 0.0381 |
| gen s/answer vqa/mismatched | 0.0364 |
| gen s/answer vqa/no_image | 0.0197 |

Mixture draw counts: {'caption_alignment': {'caption_examples': 36000}, 'visual_instruction': {'presence': 6071, 'counting': 3006, 'caption': 2923}, 'natural_vqa': {'vqa': 4160, 'presence': 600, 'counting': 608, 'caption': 632}}