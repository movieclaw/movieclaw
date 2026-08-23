"""`/Persons` 与 `/Search/Hints` 的协议形态，以及搜索口径对齐后的 /Items 行为。

背景：2026-08-22 抓包发现 Infuse 的搜索页并发打 `/Items?searchTerm=` 和
`/Persons?searchTerm=`，后者当时没有实现，请求掉到前端 catch-all 返回一整页
HTML 404，搜索页整体不可用。这里的断言既锁形态，也锁"不要再退回 HTML"。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from jellyfin.helpers import jf_login
from movieclaw_jellyfin.ids import item_guid, library_guid


class TestPersons:
    def test_returns_query_result_not_html(self, client: TestClient) -> None:
        token = jf_login(client)
        resp = client.get("/Persons", params={"ApiKey": token})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        body = resp.json()
        assert set(body) == {"Items", "TotalRecordCount", "StartIndex"}
        # 播种了 3 位影人，全部挂在电影上
        assert body["TotalRecordCount"] == 3
        assert {i["Type"] for i in body["Items"]} == {"Person"}

    def test_person_dto_shape(self, client: TestClient) -> None:
        token = jf_login(client)
        body = client.get(
            "/Persons", params={"ApiKey": token, "searchTerm": "迪卡普里奥"}
        ).json()
        assert body["TotalRecordCount"] == 1
        dto = body["Items"][0]
        assert dto["Name"] == "莱昂纳多·迪卡普里奥"
        assert dto["Type"] == "Person"
        assert dto["MediaType"] == "Unknown"
        assert dto["IsFolder"] is False
        assert dto["OriginalTitle"] == "Leonardo DiCaprio"
        assert dto["ImageTags"]["Primary"]
        assert len(dto["Id"]) == 32

    def test_search_term_matches_name_only(self, client: TestClient) -> None:
        """NameContains 只看 name 列，不看 original_name（PeopleRepository.cs:341）。"""
        token = jf_login(client)
        by_name = client.get(
            "/Persons", params={"ApiKey": token, "searchTerm": "诺兰"}
        ).json()
        assert [i["Name"] for i in by_name["Items"]] == ["克里斯托弗·诺兰"]
        by_original = client.get(
            "/Persons", params={"ApiKey": token, "searchTerm": "Nolan"}
        ).json()
        assert by_original["TotalRecordCount"] == 0

    def test_person_types_filter(self, client: TestClient) -> None:
        token = jf_login(client)
        directors = client.get(
            "/Persons", params={"ApiKey": token, "personTypes": "Director"}
        ).json()
        assert [i["Name"] for i in directors["Items"]] == ["克里斯托弗·诺兰"]
        actors = client.get(
            "/Persons", params={"ApiKey": token, "personTypes": "Actor"}
        ).json()
        assert actors["TotalRecordCount"] == 2
        # 未知 PersonKind 静默丢弃 → 不产生过滤条件
        unknown = client.get(
            "/Persons", params={"ApiKey": token, "personTypes": "Composer"}
        ).json()
        assert unknown["TotalRecordCount"] == 3

    def test_exclude_person_types(self, client: TestClient) -> None:
        token = jf_login(client)
        body = client.get(
            "/Persons", params={"ApiKey": token, "excludePersonTypes": "Director"}
        ).json()
        assert body["TotalRecordCount"] == 2

    def test_appears_in_item_id(self, client: TestClient, seeded: dict) -> None:
        token = jf_login(client)
        on_movie = client.get(
            "/Persons",
            params={"ApiKey": token, "appearsInItemId": item_guid(seeded["movie"])},
        ).json()
        assert on_movie["TotalRecordCount"] == 3
        on_show = client.get(
            "/Persons",
            params={"ApiKey": token, "appearsInItemId": item_guid(seeded["show"])},
        ).json()
        assert on_show["TotalRecordCount"] == 0

    def test_parent_id_scopes_to_library(self, client: TestClient, seeded: dict) -> None:
        token = jf_login(client)
        movie_lib = client.get(
            "/Persons",
            params={"ApiKey": token, "parentId": library_guid(seeded["movie_lib"])},
        ).json()
        assert movie_lib["TotalRecordCount"] == 3
        tv_lib = client.get(
            "/Persons",
            params={"ApiKey": token, "parentId": library_guid(seeded["tv_lib"])},
        ).json()
        assert tv_lib["TotalRecordCount"] == 0

    def test_paging(self, client: TestClient) -> None:
        token = jf_login(client)
        body = client.get(
            "/Persons", params={"ApiKey": token, "startIndex": 1, "limit": 1}
        ).json()
        assert body["TotalRecordCount"] == 3
        assert body["StartIndex"] == 1
        assert len(body["Items"]) == 1

    def test_name_starts_with(self, client: TestClient) -> None:
        token = jf_login(client)
        body = client.get(
            "/Persons", params={"ApiKey": token, "nameStartsWith": "克"}
        ).json()
        assert [i["Name"] for i in body["Items"]] == ["克里斯托弗·诺兰"]

    def test_is_favorite_yields_empty(self, client: TestClient) -> None:
        """本层不落人物收藏，IsFavorite 应筛成空而不是被忽略。"""
        token = jf_login(client)
        body = client.get(
            "/Persons", params={"ApiKey": token, "isFavorite": "true"}
        ).json()
        assert body["TotalRecordCount"] == 0
        assert body["Items"] == []

    def test_get_by_name(self, client: TestClient) -> None:
        token = jf_login(client)
        resp = client.get("/Persons/克里斯托弗·诺兰", params={"ApiKey": token})
        assert resp.status_code == 200
        assert resp.json()["Name"] == "克里斯托弗·诺兰"

    def test_get_by_name_unknown_is_404(self, client: TestClient) -> None:
        token = jf_login(client)
        resp = client.get("/Persons/查无此人", params={"ApiKey": token})
        assert resp.status_code == 404
        assert resp.text == ""

    def test_get_by_name_requires_exact_match(self, client: TestClient) -> None:
        """子串命中不算——路由段是姓名，语义是精确取人。"""
        token = jf_login(client)
        assert client.get("/Persons/诺兰", params={"ApiKey": token}).status_code == 404

    def test_requires_auth(self, client: TestClient) -> None:
        assert client.get("/Persons").status_code == 401

    def test_pascal_case_query_keys(self, client: TestClient) -> None:
        """PascalCase 客户端（Infuse 就是）发的键要能被归一化中间件认出来。"""
        token = jf_login(client)
        body = client.get(
            "/Persons", params={"ApiKey": token, "SearchTerm": "诺兰", "Limit": 5}
        ).json()
        assert body["TotalRecordCount"] == 1


class TestSearchHints:
    def test_shape(self, client: TestClient) -> None:
        token = jf_login(client)
        resp = client.get(
            "/Search/Hints", params={"ApiKey": token, "searchTerm": "breaking"}
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        body = resp.json()
        assert set(body) == {"SearchHints", "TotalRecordCount"}
        assert body["TotalRecordCount"] == 1
        hint = body["SearchHints"][0]
        assert hint["Name"] == "绝命毒师"
        assert hint["Type"] == "Series"
        assert hint["IsFolder"] is True
        assert hint["ProductionYear"] == 2008
        assert hint["Status"] == "Ended"
        # 老客户端读 ItemId，新客户端读 Id，两个都要给且相等
        assert hint["ItemId"] == hint["Id"]

    def test_missing_term_is_400(self, client: TestClient) -> None:
        token = jf_login(client)
        assert client.get("/Search/Hints", params={"ApiKey": token}).status_code == 400
        assert (
            client.get(
                "/Search/Hints", params={"ApiKey": token, "searchTerm": "   "}
            ).status_code
            == 400
        )

    def test_episode_hint_carries_index_numbers(self, client: TestClient) -> None:
        token = jf_login(client)
        body = client.get(
            "/Search/Hints", params={"ApiKey": token, "searchTerm": "Pilot"}
        ).json()
        assert body["TotalRecordCount"] == 1
        hint = body["SearchHints"][0]
        assert hint["Type"] == "Episode"
        assert (hint["ParentIndexNumber"], hint["IndexNumber"]) == (1, 1)
        assert hint["Series"] == "绝命毒师"

    def test_includes_people_by_default(self, client: TestClient) -> None:
        token = jf_login(client)
        body = client.get(
            "/Search/Hints", params={"ApiKey": token, "searchTerm": "诺兰"}
        ).json()
        assert [h["Type"] for h in body["SearchHints"]] == ["Person"]

    def test_include_people_false(self, client: TestClient) -> None:
        token = jf_login(client)
        body = client.get(
            "/Search/Hints",
            params={"ApiKey": token, "searchTerm": "诺兰", "includePeople": "false"},
        ).json()
        assert body["TotalRecordCount"] == 0

    def test_include_media_false_keeps_only_by_name_types(
        self, client: TestClient
    ) -> None:
        token = jf_login(client)
        body = client.get(
            "/Search/Hints",
            params={"ApiKey": token, "searchTerm": "盗梦", "includeMedia": "false"},
        ).json()
        assert body["TotalRecordCount"] == 0

    def test_include_item_types_narrows(self, client: TestClient) -> None:
        token = jf_login(client)
        body = client.get(
            "/Search/Hints",
            params={
                "ApiKey": token,
                "searchTerm": "绝命",
                "includeItemTypes": "Episode",
            },
        ).json()
        assert body["TotalRecordCount"] == 0

    def test_parent_id_scopes(self, client: TestClient, seeded: dict) -> None:
        token = jf_login(client)
        body = client.get(
            "/Search/Hints",
            params={
                "ApiKey": token,
                "searchTerm": "breaking",
                "parentId": library_guid(seeded["movie_lib"]),
            },
        ).json()
        assert body["TotalRecordCount"] == 0

    def test_requires_auth(self, client: TestClient) -> None:
        assert client.get("/Search/Hints", params={"searchTerm": "x"}).status_code == 401


class TestItemsSearchAlignment:
    """/Items?searchTerm= 的口径对齐（BaseItemRepository.TranslateQuery.cs:178）。"""

    def test_episode_title_is_searchable(self, client: TestClient) -> None:
        """分集有自己的 Name，搜集名能单独命中——这是对齐后新增的能力。"""
        token = jf_login(client)
        body = client.get(
            "/Items",
            params={
                "ApiKey": token,
                "searchTerm": "Pilot",
                "recursive": "true",
                "includeItemTypes": "Episode",
            },
        ).json()
        assert body["TotalRecordCount"] == 1
        assert body["Items"][0]["Name"] == "Pilot"

    def test_series_hit_does_not_drag_in_its_episodes(self, client: TestClient) -> None:
        """剧名命中不会把整季集数一并带出：Season/Episode 只比对自己的 Name。"""
        token = jf_login(client)
        body = client.get(
            "/Items",
            params={
                "ApiKey": token,
                "searchTerm": "breaking",
                "recursive": "true",
                "includeItemTypes": "Movie,Series,Episode",
            },
        ).json()
        assert [i["Type"] for i in body["Items"]] == ["Series"]

    def test_original_title_match(self, client: TestClient) -> None:
        token = jf_login(client)
        body = client.get(
            "/Items",
            params={"ApiKey": token, "searchTerm": "INCEPTION", "recursive": "true"},
        ).json()
        assert [i["Name"] for i in body["Items"]] == ["盗梦空间"]

    def test_person_type_included_on_demand(self, client: TestClient) -> None:
        """includeItemTypes 点名 Person 时才产出人物（ItemsByName 语义）。"""
        token = jf_login(client)
        without = client.get(
            "/Items",
            params={
                "ApiKey": token,
                "searchTerm": "诺兰",
                "recursive": "true",
                "includeItemTypes": "Movie,Series",
            },
        ).json()
        assert without["TotalRecordCount"] == 0
        with_person = client.get(
            "/Items",
            params={
                "ApiKey": token,
                "searchTerm": "诺兰",
                "recursive": "true",
                "includeItemTypes": "Movie,Series,Person",
            },
        ).json()
        assert [i["Type"] for i in with_person["Items"]] == ["Person"]
