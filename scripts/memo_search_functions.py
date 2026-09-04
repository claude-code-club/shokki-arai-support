"""第22課題(検索できるDB)のメモ保存・検索を、PostgreSQL最小権限化(PR #29)の
SECURITY DEFINER関数として提供する定義。

PR #29(scripts/least_privilege_lib.py、関数一覧・GRANT・ロールバックへ追加)と
PR #30(streamlit/db.py、この2関数を呼び出す側／テスト)の両方から参照される
共有モジュール。scripts/target_identity.pyと同じく、両ブランチへバイト単位で
同一の内容を配置する(内容が完全一致すればgit上のコンフリクトは発生しない)。

record_with_memo_for_tenant()・search_records_for_tenant()とも、既存の
load_dates_for_tenant()等(least_privilege_lib.py)と同じ設計方針に従う。

- LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
  (search_path乗っ取り防止のため、テーブル参照は常にpublic.*で完全修飾する)
- pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true)を呼び、
  RLSポリシー(records_tenant_isolation)が正しく世帯単位に絞り込めるようにする
- keywordのSQL LIKEワイルドカード(%・_・バックスラッシュ)は関数内でエスケープする
  (呼び出し元の実装(Python側db.py・将来の他クライアント)を信頼せず、関数自身が
  安全であることを保証する。defense in depth)
- p_orderの検証(asc/descのみ許可、NULLも拒否)、p_memo・p_keywordの長さ
  (200文字・100文字)・制御文字の検証も関数内で行う(ChatGPT監査2026-09-04
  Highの指摘を反映。streamlit/storage.pyの`_validate_memo()`/
  `_validate_search_keyword()`と同じ上限・同じ判定基準を、DB関数側でも
  独立に強制する。app_runtime資格情報を持つ呼び出し元がPython層を経由せず
  直接関数を呼んだ場合でも、同じ制限が適用される)
- エラーはすべてSQLSTATE 22023(invalid_parameter_value)で送出し、呼び出し側が
  「入力値の問題」であることを他のエラー(権限拒否等)と区別できるようにする

実行方法(このモジュール自体はDDLを実行しない、定義を提供するだけ)は
scripts/least_privilege_lib.pyのcreate_or_replace_functions()から
呼ばれるほか、テストのセットアップからも直接呼ばれる。

★PL/pgSQL関数のCREATE時チェックについて(ChatGPT監査2026-09-04 Mediumの
指摘を反映、訂正): CREATE FUNCTIONは、本体内のSQL文が参照する列が実際に
存在するかまでは検証しない(PL/pgSQLの本体は基本的な構文チェックのみで
作成でき、埋め込まれたSQLの実行計画は最初にCALLされるまで作成されない)。
したがって、public.records.memo列が存在しない状態でもこの関数定義自体は
正常にCREATEできてしまう——**CREATE成功をもって「列が存在する」という
安全性の根拠にはできない**。実際に列が存在しない状態でこの関数を
呼び出すと、実行時に`UndefinedColumn`エラーになる(実機で確認済み)。
このため、呼び出し元(scripts/migrate_to_least_privilege_schema.py)は、
`create_or_replace_functions()`を呼ぶより前に、必ず
`scripts/migrate_to_records_memo_schema.py`の`ensure_records_memo_column()`を
同じトランザクション内で実行し、`information_schema.columns`に対する
明示的な列定義検証(型・NULL許容・デフォルト値)によってmemo列の存在を
保証すること。この列定義検証こそが安全性の根拠であり、CREATE FUNCTIONの
成功可否ではない。
"""

MEMO_SEARCH_FUNCTION_DEFINITIONS = [
    r"""
    CREATE OR REPLACE FUNCTION public.record_with_memo_for_tenant(
      p_tenant_id uuid, p_record_date date, p_memo text
    ) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
    AS $$
    BEGIN
      IF p_memo IS NOT NULL THEN
        IF length(p_memo) > 200 THEN
          RAISE EXCEPTION 'メモは200文字以内で指定してください(実際: %文字)', length(p_memo)
            USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF p_memo ~ '[[:cntrl:]]' THEN
          RAISE EXCEPTION 'メモに制御文字を含めることはできません'
            USING ERRCODE = 'invalid_parameter_value';
        END IF;
      END IF;

      PERFORM pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true);
      INSERT INTO public.records (tenant_id, record_date, memo)
      VALUES (p_tenant_id, p_record_date, p_memo)
      ON CONFLICT (tenant_id, record_date) DO UPDATE SET memo = EXCLUDED.memo;
    END;
    $$;
    """,
    r"""
    CREATE OR REPLACE FUNCTION public.search_records_for_tenant(
      p_tenant_id uuid, p_keyword text, p_order text
    ) RETURNS TABLE(record_date date, memo text)
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
    AS $$
    DECLARE
      v_escaped_keyword text;
    BEGIN
      IF p_order IS NULL OR p_order NOT IN ('asc', 'desc') THEN
        RAISE EXCEPTION 'p_orderは''asc''または''desc''で指定してください: %', p_order
          USING ERRCODE = 'invalid_parameter_value';
      END IF;

      IF p_keyword IS NOT NULL AND p_keyword <> '' THEN
        IF length(p_keyword) > 100 THEN
          RAISE EXCEPTION '検索キーワードは100文字以内で指定してください(実際: %文字)', length(p_keyword)
            USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF p_keyword ~ '[[:cntrl:]]' THEN
          RAISE EXCEPTION '検索キーワードに制御文字を含めることはできません'
            USING ERRCODE = 'invalid_parameter_value';
        END IF;
      END IF;

      PERFORM pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true);

      IF p_keyword IS NOT NULL AND p_keyword <> '' THEN
        v_escaped_keyword := replace(replace(replace(p_keyword, '\', '\\'), '%', '\%'), '_', '\_');
      END IF;

      RETURN QUERY
        SELECT r.record_date, r.memo
        FROM public.records r
        WHERE r.tenant_id = p_tenant_id
          AND (v_escaped_keyword IS NULL OR r.memo ILIKE '%' || v_escaped_keyword || '%' ESCAPE '\')
        ORDER BY
          CASE WHEN p_order = 'asc' THEN r.record_date END ASC,
          CASE WHEN p_order = 'desc' THEN r.record_date END DESC;
    END;
    $$;
    """,
]

MEMO_SEARCH_FUNCTION_SIGNATURES = [
    "record_with_memo_for_tenant(uuid, date, text)",
    "search_records_for_tenant(uuid, text, text)",
]


def create_or_replace_memo_search_functions(cur):
    for definition in MEMO_SEARCH_FUNCTION_DEFINITIONS:
        cur.execute(definition)
