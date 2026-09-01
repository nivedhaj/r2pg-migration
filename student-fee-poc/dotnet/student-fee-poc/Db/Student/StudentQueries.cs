using Npgsql;

namespace StudentFeePoc.Db.Student;

public sealed record StudentRow(
    Guid Id,
    string? StudentId,
    string? Name,
    string? CourseName,
    string? TermName,
    string? SectionName
);

public static class StudentQueries
{
    public static async Task<IReadOnlyList<StudentRow>> GetRecentStudentsAsync(
        NpgsqlDataSource dataSource,
        int limit = 10)
    {
        const string sql = """
            SELECT
                s.id,
                s.student_id,
                s.name,
                se.enrollment->>'CourseName' AS course_name,
                se.enrollment->>'TermName' AS term_name,
                se.enrollment->>'SectionName' AS section_name
            FROM student s
            LEFT JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(s.enrollments) = 'array' THEN s.enrollments ELSE '[]'::jsonb END
            ) AS se(enrollment) ON TRUE
            ORDER BY s.name NULLS LAST, s.student_id NULLS LAST
            LIMIT @limit;
            """;

        var students = new List<StudentRow>();
        await using var command = dataSource.CreateCommand(sql);
        command.Parameters.AddWithValue("limit", limit);

        await using var reader = await command.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            students.Add(new StudentRow(
                reader.GetGuid(0),
                reader.IsDBNull(1) ? null : reader.GetString(1),
                reader.IsDBNull(2) ? null : reader.GetString(2),
                reader.IsDBNull(3) ? null : reader.GetString(3),
                reader.IsDBNull(4) ? null : reader.GetString(4),
                reader.IsDBNull(5) ? null : reader.GetString(5)
            ));
        }

        return students;
    }
}
