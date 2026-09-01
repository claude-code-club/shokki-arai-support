"""Claude APIで、記録を続けている本人への一言コメントを生成する（第21課題）。"""

import os
import sys

import anthropic


def get_encouragement(streak, best_streak):
    """現在の連続記録日数から、短い応援コメントを1つ生成して返す。

    APIキー未設定やAPIエラー時は None を返す（呼び出し側でフォールバック表示する）。
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=60,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "あなたは『食器洗いサポート』という日々の習慣アプリの応援担当です。"
                        f"ユーザーは食器洗いを{streak}日連続で記録しました（自己ベストは{best_streak}日）。"
                        "この人に向けて、絵文字を1つ含む、20〜40文字程度の短く前向きな日本語の一言コメントだけを返してください。"
                        "説明や前置きは不要です。"
                    ),
                }
            ],
        )
        return message.content[0].text.strip()
    except Exception as e:
        # 外部APIの失敗理由を問わず、ここで必ず吸収してアプリを落とさない。
        # 原因の見当がつく程度の情報(例外の型名)だけを標準エラー出力
        # （ホスティング先のログ）に残す。例外メッセージ本体は出さない
        # (第21回: SaaSのセキュリティ堅牢化。APIキー等が意図せず含まれないようにする)。
        print(f"[encourage] Claude API呼び出し失敗: {type(e).__name__}", file=sys.stderr)
        return None
