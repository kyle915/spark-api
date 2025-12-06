import pytest
from events.tests.base import EventsGraphQLTestCase
from events.models import TimeZone
from config.schema_client import schema_clients

@pytest.mark.django_db(transaction=True)
class TestTimeZoneQueries(EventsGraphQLTestCase):
    
    @pytest.fixture(autouse=True)
    def setup(self):
        self.schema = schema_clients
        self.system_user = self.get_system_user()
        
        self.timezone1 = TimeZone.objects.create(
            name="Americas/Mexico_City",
            code="MDT",
            offset=-6,
            created_by=self.system_user
        )
        self.timezone2 = TimeZone.objects.create(
            name="Europe/London",
            code="GMT",
            offset=0,
            created_by=self.system_user
        )
        
        # Setup tenant and user for authenticated request
        self.tenant = self.create_tenant()
        self.user_role = self.create_role("Client", 3)
        self.user = self.create_user("client", "client@spark.local", self.user_role)
        self.tenanted_user = self.create_tenanted_user(self.user, self.tenant)

    @pytest.mark.asyncio
    async def test_timezones_query(self):
        query = """
        query {
            timezones {
                edges {
                    node {
                        name
                        code
                        offset
                    }
                }
            }
        }
        """
        result = await self._execute_query_authenticated(query, user=self.user)
        assert result.errors is None
        
        edges = result.data["timezones"]["edges"]
        assert len(edges) == 2
        names = [edge["node"]["name"] for edge in edges]
        assert "Americas/Mexico_City" in names
        assert "Europe/London" in names

    @pytest.mark.asyncio
    async def test_public_timezones_query(self):
        query = """
        query {
            publicTimezones {
                edges {
                    node {
                        name
                        code
                        offset
                    }
                }
            }
        }
        """
        result = await self._execute_mutation(query, variables={})
        assert result.errors is None
        
        edges = result.data["publicTimezones"]["edges"]
        assert len(edges) == 2
        names = [edge["node"]["name"] for edge in edges]
        assert "Americas/Mexico_City" in names
        assert "Europe/London" in names

    @pytest.mark.asyncio
    async def test_timezones_search(self):
        query = """
        query($q: String) {
            timezones(q: $q) {
                edges {
                    node {
                        name
                    }
                }
            }
        }
        """
        result = await self._execute_query_authenticated(query, variables={"q": "Mexico"}, user=self.user)
        assert result.errors is None
        
        edges = result.data["timezones"]["edges"]
        assert len(edges) == 1
        assert edges[0]["node"]["name"] == "Americas/Mexico_City"

    @pytest.mark.asyncio
    async def test_create_timezone(self):
        mutation = """
        mutation CreateTimeZone($input: CreateTimeZoneInput!) {
            createTimezone(input: $input) {
                success
                message
                timezone {
                    name
                    code
                    offset
                }
            }
        }
        """
        variables = {
            "input": {
                "name": "Asia/Tokyo",
                "code": "JST",
                "offset": 9
            }
        }
        
        result = await self._execute_mutation(mutation, variables, user=self.user)
        assert result.errors is None
        assert result.data["createTimezone"]["success"] is True
        assert result.data["createTimezone"]["timezone"]["name"] == "Asia/Tokyo"
        
        # Verify db
        timezone = await TimeZone.objects.aget(name="Asia/Tokyo")
        assert timezone.code == "JST"
        assert timezone.offset == 9

    @pytest.mark.asyncio
    async def test_update_timezone(self):
        mutation = """
        mutation UpdateTimeZone($input: UpdateTimeZoneInput!) {
            updateTimezone(input: $input) {
                success
                message
                timezone {
                    name
                    code
                    offset
                }
            }
        }
        """
        variables = {
            "input": {
                "id": str(self.timezone1.id),
                "name": "Americas/Mexico_City_Updated",
                "code": "CDMX",
                "offset": -5
            }
        }
        
        result = await self._execute_mutation(mutation, variables, user=self.user)
        assert result.errors is None
        assert result.data["updateTimezone"]["success"] is True
        assert result.data["updateTimezone"]["timezone"]["name"] == "Americas/Mexico_City_Updated"
        
        # Verify db
        await self.timezone1.arefresh_from_db()
        assert self.timezone1.name == "Americas/Mexico_City_Updated"
        assert self.timezone1.code == "CDMX"
        assert self.timezone1.offset == -5


