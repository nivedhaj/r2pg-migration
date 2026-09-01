using System.Text.Json;
using System.Text.Json.Nodes;
using Npgsql;

namespace StudentFeePoc.Db.Student;

public static class StudentQueries
{
    public static async Task<JsonObject> GetStudentsPayloadAsync(
        NpgsqlDataSource dataSource,
        bool activeStudentsOnly = false,
        bool todaysAbsenteesOnly = false,
        int limit = 50)
    {
        const string sql = """
            SELECT jsonb_build_object(
                'id', s.id,
                'name', s.name,
                'dob', to_char(s.dob AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS'),
                'gender', s.gender::text,
                'email', s.email,
                'mobile', s.mobile,
                'parentEmails', jsonb_build_array(
                    COALESCE(s.father->>'Email', ''),
                    COALESCE(s.mother->>'Email', ''),
                    COALESCE(s.guardian->>'Email', '')
                ),
                'parentMobiles', jsonb_build_array(
                    COALESCE(s.father->>'Mobile', ''),
                    COALESCE(s.mother->>'Mobile', ''),
                    COALESCE(s.guardian->>'Mobile', '')
                ),
                'father', COALESCE(s.father, '{}'::jsonb),
                'mother', COALESCE(s.mother, '{}'::jsonb),
                'guardian', COALESCE(s.guardian, '{}'::jsonb),
                'address', CASE WHEN jsonb_typeof(s.addresses) = 'array' AND jsonb_array_length(s.addresses) > 0 THEN s.addresses->0 ELSE NULL END,
                'photoURL', s.photo_url,
                'instId', s.inst_id,
                'studentId', s.student_id,
                'status', CASE WHEN s.status = 'Active' THEN 1 WHEN s.status = 'Disabled' THEN 99 ELSE -1 END,
                'statusAsString', s.status::text,
                'courseId', se.enrollment->>'CourseId',
                'courseSortIndex', COALESCE((se.enrollment->>'CourseSortIndex')::int, 0),
                'courseName', se.enrollment->>'CourseName',
                'branch', se.enrollment->>'Branch',
                'termName', se.enrollment->>'TermName',
                'sectionName', se.enrollment->>'SectionName',
                'rollNo', COALESCE((se.enrollment->>'RollNoAsInt')::int, (se.enrollment->>'RollNo')::int, 0),
                'doa', COALESCE(se.enrollment->>'DOA', '0001-01-01T00:00:00'),
                'admissionNo', se.enrollment->>'AdmissionNo',
                'applicationNo', se.enrollment->>'ApplicationNo',
                'aadharNumber', s.aadhar_number,
                'marksCardTemplate', se.enrollment->>'MarksCardTemplate',
                'attendance', COALESCE(s.attendance->>'Status', 'Present'),
                'attendanceList', COALESCE(s.attendance->'List', '["Present","Present","Present","Present","Present","Present","Present","Present"]'::jsonb),
                'tags', COALESCE(to_jsonb(s.tags), '[]'::jsonb),
                'virtualId', s.virtual_id,
                'domicile', COALESCE(s.domicile, '{"caste":null,"state":null,"country":"India","motherTongue":null}'::jsonb),
                'iep', COALESCE(s.iep, '{"terms":[]}'::jsonb)
            )::text
            FROM student s
            LEFT JOIN LATERAL (
                SELECT enrollment 
                FROM jsonb_array_elements(
                    CASE WHEN jsonb_typeof(s.enrollments) = 'array' THEN s.enrollments ELSE '[]'::jsonb END
                ) AS enrollment 
                LIMIT 1
            ) se ON TRUE
            WHERE (@activeOnly::boolean = FALSE OR s.status = 'Active')
            ORDER BY s.name NULLS LAST, s.student_id NULLS LAST
            LIMIT @limit;
            """;

        var jsonArray = new JsonArray();
        await using var command = dataSource.CreateCommand(sql);
        command.Parameters.AddWithValue("activeOnly", activeStudentsOnly);
        command.Parameters.AddWithValue("limit", limit);

        await using var reader = await command.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            var jsonText = reader.GetString(0);
            var node = JsonNode.Parse(jsonText);
            if (node is not null)
            {
                NormalizeKeysToCamelCase(node);
                jsonArray.Add(node);
            }
        }

        var result = new JsonObject
        {
            ["data"] = jsonArray
        };
        return result;
    }

    private static void NormalizeKeysToCamelCase(JsonNode node)
    {
        if (node is JsonObject obj)
        {
            var properties = obj.ToList();
            foreach (var kvp in properties)
            {
                var camelKey = ToCamelCase(kvp.Key);
                if (camelKey != kvp.Key)
                {
                    obj.Remove(kvp.Key);
                    obj[camelKey] = kvp.Value;
                }
                if (kvp.Value is not null)
                {
                    NormalizeKeysToCamelCase(kvp.Value);
                }
            }
        }
        else if (node is JsonArray arr)
        {
            foreach (var item in arr)
            {
                if (item is not null)
                {
                    NormalizeKeysToCamelCase(item);
                }
            }
        }
    }

    private static string ToCamelCase(string str)
    {
        if (string.IsNullOrEmpty(str) || char.IsLower(str[0]))
            return str;

        if (str.Equals("photoURL", StringComparison.OrdinalIgnoreCase))
            return "photoURL";
        if (str.Equals("instId", StringComparison.OrdinalIgnoreCase))
            return "instId";
        if (str.Equals("studentId", StringComparison.OrdinalIgnoreCase))
            return "studentId";
        if (str.Equals("courseId", StringComparison.OrdinalIgnoreCase))
            return "courseId";
        if (str.Equals("ownerId", StringComparison.OrdinalIgnoreCase))
            return "ownerId";
        if (str.Equals("parentId", StringComparison.OrdinalIgnoreCase))
            return "parentId";
        if (str.Equals("virtualId", StringComparison.OrdinalIgnoreCase))
            return "virtualId";
        if (str.Equals("DOB", StringComparison.OrdinalIgnoreCase))
            return "dob";
        if (str.Equals("PAN", StringComparison.OrdinalIgnoreCase))
            return "pan";
        if (str.Equals("DOA", StringComparison.OrdinalIgnoreCase))
            return "doa";

        return char.ToLowerInvariant(str[0]) + str[1..];
    }
}
