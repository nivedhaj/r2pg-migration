using Npgsql;
using StudentFeePoc.Db.Fee;
using StudentFeePoc.Db.Student;
using Xunit;

namespace StudentFeePoc.Tests;

public class DbReadTests : IClassFixture<TestFixture>
{
    private readonly TestFixture _fixture;

    public DbReadTests(TestFixture fixture)
    {
        _fixture = fixture;
    }

    [Fact]
    public async Task CanConnectToDatabase()
    {
        await using var cmd = _fixture.DataSource.CreateCommand("SELECT 1;");
        var result = await cmd.ExecuteScalarAsync();
        Assert.Equal(1, Convert.ToInt32(result));
    }

    [Fact]
    public async Task CanQueryCampusTrackStudentsPayload()
    {
        var response = await StudentQueries.GetStudentsPayloadAsync(_fixture.DataSource, limit: 5);
        Assert.NotNull(response);
        Assert.True(response.ContainsKey("data"));
        var dataArray = response["data"]?.AsArray();
        Assert.NotNull(dataArray);
        if (dataArray.Count > 0)
        {
            var first = dataArray[0]?.AsObject();
            Assert.NotNull(first);
            Assert.True(first.ContainsKey("id"));
            Assert.True(first.ContainsKey("name"));
            Assert.True(first.ContainsKey("studentId"));
            Assert.True(first.ContainsKey("father"));
            Assert.True(first.ContainsKey("mother"));
            Assert.True(first.ContainsKey("guardian"));
            Assert.True(first.ContainsKey("courseName"));
        }
    }

    [Fact]
    public async Task CanQueryCampusTrackFeeList()
    {
        var feeList = await FeeQueries.GetFeesAsync(_fixture.DataSource, limit: 5);
        Assert.NotNull(feeList);
        Assert.NotNull(feeList.Data);
        if (feeList.Data.Count > 0)
        {
            var first = feeList.Data[0];
            Assert.NotEqual(Guid.Empty, first.Id);
            Assert.False(string.IsNullOrWhiteSpace(first.Name));
        }
    }

    [Fact]
    public async Task CanQueryCampusTrackFeeTransactionsApiPayload()
    {
        var response = await FeeTransactionQueries.GetFeeTransactionsAsync(_fixture.DataSource, limit: 5);
        Assert.NotNull(response);
        Assert.NotNull(response.Data);
        if (response.Data.Count > 0)
        {
            var first = response.Data[0];
            Assert.NotEqual(Guid.Empty, first.Id);
            Assert.NotNull(first.StudentName);
        }
    }
}
