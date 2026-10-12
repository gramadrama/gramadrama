import Foundation

package struct LTMatch: Decodable {
    package struct Replacement: Decodable { package let value: String }
    package struct Rule: Decodable {
        package struct Category: Decodable { package let id: String }
        package let id: String
        package let category: Category
    }

    package let message: String
    package let offset: Int  // w jednostkach UTF-16 (Java)
    package let length: Int
    package let replacements: [Replacement]
    package let rule: Rule
}

/// Odpowiedź `/v2/check` LanguageTool.
package struct LTResponse: Decodable {
    package struct Language: Decodable {
        package struct Detected: Decodable { package let code: String }
        package let code: String
        package let detectedLanguage: Detected?
    }

    package let matches: [LTMatch]
    package let language: Language
}

package struct LTResult {
    package let matches: [LTMatch]
    package let language: String

    package init(matches: [LTMatch], language: String) {
        self.matches = matches
        self.language = language
    }
}
