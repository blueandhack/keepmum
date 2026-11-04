"""Qwen3Guard-Gen's prompt for moderating a user message (Apache-2.0, Qwen team).

Its chat template inserts the message between these two texts and nothing else, the same for the
0.6B and the 4B. Rendered from Qwen/Qwen3Guard-Gen-0.6B at revision fada3b2f655b89601929198343c94cd2f64d93cc.
keepmum appends "Safety:" and reads the probability of the next token: Safe, Unsafe or Controversial.
Generated once from the official tokenizer; do not edit.
"""

PREFIX = "<|im_start|>user\n# Task:\n1) Evaluate whether THE LAST USER's query belongs to safe or unsafe content based on our safety policy.\n2) Identify the category of unsafe content found.\n\n<BEGIN SAFETY POLICY>\n**Safety: Safe**: The USER's query does not contain any prohibited content or content that could lead to prohibited content.\n**Safety: Unsafe**: The USER's query contains prohibited content.\n**Safety: Controversial**: The USER's query does not explicitly contain prohibited content, but its intent, context, or potential responses could be misinterpreted, misused, or exploited to generate unsafe content under certain conditions.\n<END SAFETY POLICY>\n\n<BEGIN UNSAFE CONTENT CATEGORIES>\nViolent.\nNon-violent Illegal Acts.\nSexual Content or Sexual Acts.\nPII.\nSuicide & Self-Harm.\nUnethical Acts.\nPolitically Sensitive Topics.\nCopyright Violation.\nJailbreak.\n<END UNSAFE CONTENT CATEGORIES>\n\n<BEGIN CONVERSATION>\n\nUSER: "

SUFFIX = "\n\n<END CONVERSATION>\n\nProvide your safety assessment for ONLY THE LAST **USER's query** in the above conversation:\n - The first line must be one of: 'Safety: Safe', 'Safety: Unsafe', 'Safety: Controversial'.\n - The second line should start with 'Categories:' followed by a list of any unsafe content categories, separated by commas. If the content is safe, use 'Categories: None'.<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
