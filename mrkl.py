import os
import re
from dotenv import load_dotenv
from langchain.chat_models import ChatOpenAI
from langchain.llms import OpenAI
from langchain.agents import load_tools, initialize_agent, AgentType, Tool, AgentExecutor, ConversationalAgent, ZeroShotAgent
from langchain.callbacks import StreamlitCallbackHandler
from langchain.tools import DuckDuckGoSearchRun
from langchain.memory import ConversationBufferMemory, ConversationBufferWindowMemory
from langchain.chains import LLMMathChain
from langchain import LLMChain
import streamlit as st
from langchain.utilities import SerpAPIWrapper
from langchain.chains import RetrievalQA
from langchain.text_splitter import RecursiveCharacterTextSplitter, MarkdownHeaderTextSplitter
from langchain.vectorstores import FAISS, Chroma
from langchain.embeddings import OpenAIEmbeddings
from PyPDF2 import PdfReader
import tempfile
import pypdf
import json
import openai
from pathlib import Path
from langchain.docstore.document import Document
from langchain.chains.router import MultiRetrievalQAChain
from langchain.document_loaders import DirectoryLoader, PyPDFLoader, TextLoader, WebBaseLoader, UnstructuredMarkdownLoader
from langchain.chains.summarize import load_summarize_chain
from langchain import PromptTemplate
from langchain.retrievers.self_query.base import SelfQueryRetriever
from langchain.chains.query_constructor.base import AttributeInfo
import lark
from langchain.schema import Document


def on_selectbox_change():
    st.session_state.show_info = True

def reset_chat():
    st.session_state.messages = [{"roles": "assistant", "content": "How can I help you?"}]
    st.session_state.history = []
    st.session_state.search_keywords = []
    st.session_state.doc_sources = []
    st.session_state.history = None
    st.session_state.summary = None
    st.session_state.agent.clear_conversation()

    
def display_messages(messages):
    # Display all messages
    for msg in messages:
        st.chat_message(msg["roles"]).write(msg["content"])

class DBStore:
    def __init__(self, file_path, file_name):
        self.file_path = file_path
        self.file_name = os.path.splitext(file_name)[0]
        self.reader = pypdf.PdfReader(file_path)
        self.metadata = self.extract_metadata_from_pdf()
        self.embeddings = OpenAIEmbeddings()
        self.vector_store = None

    def extract_metadata_from_pdf(self):
        """Extract metadata from the PDF."""
        metadata = self.reader.metadata
        st.session_state.document_metadata = metadata
        return {
            "title": metadata.get("/Title", "").strip(),
            "author": metadata.get("/Author", "").strip(),
            "creation_date": metadata.get("/CreationDate", "").strip(),
        }
    
    def extract_pages_from_pdf(self):
        pages = []
        for page_num, page in enumerate(self.reader.pages):
            text = page.extract_text()
            if text.strip():  # Check if extracted text is not empty
                pages.append((page_num + 1, text))
        return pages

    def parse_pdf(self):
        """
        Extracts the title and text from each page of the PDF.
        :return: A tuple containing the title and a list of tuples with page numbers and extracted text.
        """
        metadata = self.extract_metadata_from_pdf()
        pages = self.extract_pages_from_pdf()
        #st.write(pages)
        #st.write(metadata)
        return pages, metadata
    
    @staticmethod
    def merge_hyphenated_words(text):
        return re.sub(r"(\w)-\n(\w)", r"\1\2", text)

    @staticmethod
    def fix_newlines(text):
        return re.sub(r"(?<!\n)\n(?!\n)", " ", text)

    @staticmethod
    def remove_multiple_newlines(text):
        return re.sub(r"\n{2,}", "\n", text)

    def clean_text(self, pages):
        cleaning_functions = [
            self.merge_hyphenated_words,
            self.fix_newlines,
            self.remove_multiple_newlines,
        ]
        cleaned_pages = []
        for page_num, text in pages:
            for cleaning_function in cleaning_functions:
                text = cleaning_function(text)
            cleaned_pages.append((page_num, text))
        return cleaned_pages

    def text_to_docs(self, text):
        doc_chunks = []
        for page_num, page in text:
            text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=1000,
                separators=["\n\n", "\n", ".", "!", "?", ",", " ", ""],
                chunk_overlap=200,
            )
            chunks = text_splitter.split_text(page)
            for i, chunk in enumerate(chunks):
                doc = Document(
                    page_content=chunk,
                    metadata={
                        "page_number": page_num,
                        "chunk": i,
                        "source": f"p{page_num}-{i}",
                        "file_name": self.file_name,
                        **self.metadata,
                    },
                )
                doc_chunks.append(doc)
            #st.write(doc_chunks)
        return doc_chunks
    
    def get_pdf_text(self):
        pages, metadata = self.parse_pdf()  # We only need the pages from the tuple
        cleaned_text_pdf = self.clean_text(pages)
        document_chunks = self.text_to_docs(cleaned_text_pdf)
        return document_chunks

    def get_vectorstore(self):
        document_chunks = self.get_pdf_text()
        vector_store = FAISS.from_documents(documents=document_chunks, embedding=self.embeddings)
        #st.write(vector_store)
        return vector_store
   
class DatabaseTool:
    def __init__(self, llm, vector_store, metadata=None):
        self.retrieval = RetrievalQA.from_chain_type(
            llm=llm, chain_type="stuff", retriever=vector_store.as_retriever(),
            return_source_documents=True
        )
        self.metadata = metadata

    def get_description(self):
        base_description = "Always useful for finding the exactly written answer to the question by looking into a collection of documents."
        if self.metadata:
            title = self.metadata.get("/Title")
            author = self.metadata.get("/Author")
            subject = self.metadata.get("/Subject")
            if author:
                return f"{base_description} This tool is currently loaded with '{title}' by '{author}', and has a topic of '{subject}'. Input should be a query, not referencing any obscure pronouns from the conversation before that will pull out relevant information from the database."
            else:
                return f"{base_description} This tool is currently loaded with '{title}', and has a topic of {subject}. Input should be a query, not referencing any obscure pronouns from the conversation before that will pull out relevant information from the database."
        return base_description


    def run(self, query: str):
        output = self.retrieval(query)
        st.session_state.doc_sources = output['source_documents']
        return output['result']

class BR18_DB:
    def __init__(self, llm, folder_path: str):
        self.llm = llm
        self.folder_path = folder_path
        self.md_paths = self.load_documents()  # Renamed from pdf_paths to md_paths
        self.embeddings = OpenAIEmbeddings()
        self.vectorstore = self.create_vectorstore()
        self.retriever = self.create_retriever()

    def load_documents(self):
        md_paths = list(Path(self.folder_path).rglob("*.md"))
        documents = []
        for path in md_paths:
            loader = TextLoader(str(path))
            data = loader.load()
            documents.extend(data)  # Assuming data is a list of Document objects
        #st.text(documents)
        return documents
    
    def split_and_chunk_text(self, markdown_document: Document):
        # Extract the markdown text from the Document object
        markdown_text = markdown_document.page_content
        #st.write(f"Type of markdown_document: {type(markdown_document)}")
        #st.markdown(markdown_text)
        
        # Define headers to split on
        headers_to_split_on = [
            ("#", "Header 1"),
            ("##", "Header 2"),
            ("###", "Header 3"),
            ("####", "Header 4"),
        ]
        
        # Initialize MarkdownHeaderTextSplitter
        markdown_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=headers_to_split_on)
        #st.write(markdown_splitter)
        
        # Split the document by headers
        md_header_splits = markdown_splitter.split_text(markdown_text)
        #st.write(md_header_splits)
        #st.write(type(md_header_splits[0]))
        
        # Define chunk size and overlap
        chunk_size = 1000
        chunk_overlap = 200
        
        # Initialize RecursiveCharacterTextSplitter
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size, chunk_overlap=chunk_overlap
        )
        
        # Split the header-split documents into chunks
        all_splits = text_splitter.split_documents(md_header_splits)

        
        # Output for debugging

        
        return all_splits
    
    def process_all_documents(self):
        all_processed_splits = []
        for markdown_document in self.md_paths:
            processed_splits = self.split_and_chunk_text(markdown_document)
            all_processed_splits.extend(processed_splits)
        return all_processed_splits
    
    def create_vectorstore(self):
        persist_dir = "./chroma_db"

        # Check if the directory exists
        if Path(persist_dir).exists():
            # Load the vector store from disk
            br18_vectorstore = Chroma(persist_directory=persist_dir, embedding_function=self.embeddings)
            st.write("Loaded BR18 vectorstore from disk.")
        else:
            # Use the process_all_documents method to get all the processed splits
            all_splits = self.process_all_documents()

            # Create and save the vector store to disk
            br18_vectorstore = Chroma.from_documents(documents=all_splits, embedding_function=self.embeddings, persist_directory=persist_dir)

        # Store the vector store in the session state
        st.session_state.br18_vectorstore = br18_vectorstore

        return br18_vectorstore
    

    def create_retriever(self):
        metadata_field_info = [
            AttributeInfo(
                name="Header 2",
                description="Header of a section containing relevant words to its information",
                type="string or list[string]",
            ),
            AttributeInfo(
                name="Header 3",
                description="Header of a section containing relevant words to its information",
                type="string or list[string]",
            ),
            AttributeInfo(
                name="Header 4",
                description="Header of a section containing relevant words to its information",
                type="string or list[string]",
            ),
        ]
        document_content_description = "Major sections of the document, organized by hierarchical levels of headers."
        retriever = SelfQueryRetriever.from_llm(self.llm, self.vectorstore, document_content_description, metadata_field_info, verbose=True)
        #st.write(retriever)    


        query = "What are the regulations regarding building permit application?"
        #st.write(retriever.get_relevant_documents(query))
        return retriever

    def run(self, query: str):
        retrieval = RetrievalQA.from_chain_type(
            llm=self.llm, chain_type="map_reduce", retriever=self.retriever, return_source_documents=True, verbose = True
        )
        output = retrieval(query)
        st.session_state.doc_sources = output['source_documents']
        return output['result']

class USC_DB:
    def __init__(self, llm, folder_path: str, index_path: str = "faiss_index"):
        self.llm = llm
        self.folder_path = folder_path
        self.index_path = index_path
        self.pdf_paths = self.load_documents()
        self.embeddings = OpenAIEmbeddings()
        self.vectorstore = self.create_vectorstore()

    def load_documents(self):
        pdf_paths = list(Path(self.folder_path).rglob("*.pdf"))
        return [str(path) for path in pdf_paths]
    
    
    def extract_metadata_from_pdf(self, pdf_path):
        """Extract metadata from the PDF."""
        reader = pypdf.PdfReader(pdf_path)
        metadata = reader.metadata
        return {
            "title": metadata.get("/Title", "").strip(),
            "author": metadata.get("/Author", "").strip(),
            "creation_date": metadata.get("/CreationDate", "").strip(),
        }

    def extract_pages_from_pdf(self, pdf_path):
        reader = pypdf.PdfReader(pdf_path)
        pages = []
        #st.write(pages)
        for page_num, page in enumerate(reader.pages):
            text = page.extract_text()
            if text.strip():  # Check if extracted text is not empty
                pages.append((page_num + 1, text))
        return pages
    
    def parse_pdf(self, pdf_path):
        pages = self.extract_pages_from_pdf(pdf_path)
        metadata = self.extract_metadata_from_pdf(pdf_path)
        return pages, metadata
    
    @staticmethod
    def merge_hyphenated_words(text):
        return re.sub(r"(\w)-\n(\w)", r"\1\2", text)

    @staticmethod
    def fix_newlines(text):
        return re.sub(r"(?<!\n)\n(?!\n)", " ", text)

    @staticmethod
    def remove_multiple_newlines(text):
        return re.sub(r"\n{2,}", "\n", text)

    def clean_text(self, pages):
        cleaning_functions = [
            self.merge_hyphenated_words,
            self.fix_newlines,
            self.remove_multiple_newlines,
        ]
        cleaned_pages = []
        #st.write(cleaned_pages)
        #st.write(pages)
        for page_num, text in pages:
            for cleaning_function in cleaning_functions:
                text = cleaning_function(text)
            cleaned_pages.append((page_num, text))
        return cleaned_pages

    def text_to_docs(self, cleaned_text):
        doc_chunks = []
        for page_num, page in cleaned_text:
            text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=1500,
                separators=["\n\n", "\n", ".", "!", "?", ",", " ", ""],
                chunk_overlap=150,
            )
            chunks = text_splitter.split_text(page)
            for i, chunk in enumerate(chunks):
                doc = Document(
                    page_content=chunk,
                    metadata={
                        "page_number": page_num,
                        "chunk": i,
                        "source": f"p{page_num}-{i}",
                        "file_name": self.metadata["file_name"],  # Assuming pdf_path is accessible here
                        **self.metadata,
                    },
                )
                doc_chunks.append(doc)
        st.write(doc_chunks)
        return doc_chunks

    def load_and_process_documents(self):
        documents = []
        for pdf_path in self.pdf_paths:
            #st.write(f"Processing: {pdf_path}")
            pages, self.metadata = self.parse_pdf(pdf_path)
            file_name = Path(pdf_path).stem
            self.metadata["file_name"] = file_name
            cleaned_pages = self.clean_text(pages)
            docs = self.text_to_docs(cleaned_pages)
            documents.extend(docs)
        return documents

    def create_vectorstore(self, index_path="faiss_index"):
        if os.path.exists(index_path):
            # Load the existing FAISS index from disk
            usc_vectorstore = FAISS.load_local(index_path, self.embeddings)
            st.write("Loaded USC vectorstore from disk.")
        else:
            # Create a new FAISS index
            st.write("Creating new vectorstore...")
            documents = self.load_and_process_documents()
            usc_vectorstore = FAISS.from_documents(documents, self.embeddings)
            
            # Save the new FAISS index to disk
            usc_vectorstore.save_local(index_path)
            st.write(f"Saved vectorstore to {index_path}.")

        st.session_state.usc_vectorstore = usc_vectorstore
        return usc_vectorstore

    def run(self, query: str):
        retrieval = RetrievalQA.from_chain_type(
            llm=self.llm, chain_type="map_reduce", retriever=self.vectorstore.as_retriever(), return_source_documents=True
        )
        output = retrieval(query)
        st.session_state.doc_sources = output['source_documents']
        return output['result']

class SummarizationTool():
    def __init__(self, document_chunks):
        self.llm = ChatOpenAI(
            temperature=0, 
            streaming=True,
            model_name="gpt-3.5-turbo"
        )
        self.document_chunks = document_chunks
        self.map_prompt_template, self.combine_prompt_template = self.load_prompts()
        self.chain = self.load_summarize_chain()

    def load_prompts(self):
        map_prompt = '''
        Summarize the following text in a clear and concise way:
        TEXT:`{text}`
        Brief Summary:
        '''
        combine_prompt = '''
        Generate a summary of the following text that includes the following elements:

        * A title that accurately reflects the content of the text.
        * An introduction paragraph that provides an overview of the topic.
        * Bullet points that list the key points of the text.

        Text:`{text}`
        '''
        map_prompt_template = PromptTemplate(template=map_prompt, input_variables=["text"])
        combine_prompt_template = PromptTemplate(template=combine_prompt, input_variables=["text"])
        return map_prompt_template, combine_prompt_template
    
    def load_summarize_chain(self):
        return load_summarize_chain(
            llm=self.llm,
            chain_type='map_reduce',
            map_prompt=self.map_prompt_template,
            combine_prompt=self.combine_prompt_template,
            verbose=True
        )

    def run(self, query=None):
        return self.run_chain()

    def run_chain(self):
        return self.chain.run(self.document_chunks)

class MRKL:
    def __init__(self):
        self.tools = self.load_tools()
        self.agent, self.memory = self.load_agent()
        

    def load_tools(self):
        # Load tools
        llm = ChatOpenAI(
            temperature=0, 
            streaming=True,
            model_name="gpt-3.5-turbo"
            )
        llm_math = LLMMathChain(llm=llm)
        llm_search = DuckDuckGoSearchRun()

        current_directory = os.getcwd()
        br18_folder_path = os.path.join(current_directory, "BR18_DB")
        llm_br18 = BR18_DB(llm=llm, folder_path=br18_folder_path)  # Replace with your folder path

        usc_folder_path = os.path.join(current_directory, "USC_DB")
        usc_db = USC_DB(llm=llm, folder_path=usc_folder_path)  # Replace with your folder path

        tools = [
            Tool(
                name="Search",
                func=llm_search.run,
                description="Useful when you cannot find a clear answer by looking up the database and that you need to search the internet for information. Input should be a fully formed question based on the context of what you couldn't find and not referencing any obscure pronouns from the conversation before"
            ),
            Tool(
                name='Calculator',
                func=llm_math.run,
                description='Useful for when you need to answer questions about math.'
            ),
            Tool(
                name='BR18 Database',
                func=llm_br18.run,
                description="Always useful for when you need to answer questions about the Danish Building Regulation 18 (BR18). Input should be a fully formed question. Use this tool more often than the normal search tool"
            ),
            Tool(
                name='USC Database',
                func=usc_db.run,
                description="Always useful for when you need to answer questions about the United State Constitution and The Bills of Rights. Input should be a fully formed question. Use this tool more often than the normal search tool"
            ),
        ]

        # Only add the DatabaseTool if vector_store exists
        if st.session_state.vector_store is not None:
            metadata = st.session_state.document_metadata
            llm_database = DatabaseTool(llm=llm, vector_store=st.session_state.vector_store, metadata=metadata)

            #st.write(llm_database.get_description())

            tools.extend([
                Tool(
                    name='Document Database',
                    func=llm_database.run,
                    description=llm_database.get_description(),
                ),
            ])

        if st.session_state.get("experimental_feature", False):
            current_directory = os.getcwd()
            br18_folder_path = os.path.join(current_directory, "BR18_DB")
            llm_br18 = BR18_DB(llm=llm, folder_path=br18_folder_path)  # Replace with your folder path

            tools.append(
                Tool(
                    name='BR18 Database',
                    func=llm_br18.run,
                    description="Always useful for when you need to answer questions about the Danish Building Regulation 18 (BR18). Input should be a fully formed question. Use this tool more often than the normal search tool"
                )
            )
        return tools






    def load_agent(self):
        llm = ChatOpenAI(
            temperature=0, 
            streaming=True,
            model_name="gpt-3.5-turbo"
            )

        PREFIX ="""Assistant is a large language model trained by OpenAI. Assistant is designed to be able to assist with a wide range of tasks, from answering simple questions to providing in-depth explanations and discussions on a wide range of topics. As a language model, Assistant is able to generate human-like text based on the input it receives, allowing it to engage in natural-sounding conversations and provide responses that are coherent and relevant to the topic at hand.

        Overall, Assistant is a powerful tool that can help with a wide range of tasks and provide valuable insights and information on a wide range of topics. 

        Assistant has access to the 'BR18 Database' which contains information about the Building Regulation 2018 in Denmark. Always search in the 'BR18 Database' if the user query is related to building regulation and laws. Your answers should always be detailed while citing the clauses number within the bracket. 

        Assistant also access to the 'USC Database' which contains information about the United State Constitution and The Bills of Rights project. Always search in this database if the user query is related about the United State Constitution and The Bills of Rights
        
        Otherwise, always search for answers and relevant examples within PDF pages (documents) provided in the 'Document Database'. The 'Document Database' contains general information from uploaded documents.  
        
        If you are unable to find sufficient information you may use a general internet search to find results. However, always prioritize providing answers and examples from either the 'BR18 Database' or the 'Document Database' before resorting to general internet search.

        If the user question does not require any tools, simply kindly respond back in an assitive manner as a Final Answer

        Assistant has access to the following tools:"""

        FORMAT_INSTRUCTIONS = """
        Use the following format:
        '''
        Question: the input question you must answer
        Thought: you should always think about what to do
        Action: the action to take, should be one of [{tool_names}] or if no tool is needed, then skip to Final Thought
        Action Input: the input to the action. 
        Observation: the result of the action 

        If your Thought mentions multiple tools, your next Action should correspond to the first tool you mentioned.
        If your Thought does not mentions any tools, skip to the Final Thought

        ... (this Thought/Action/Action Input/Observation can repeat N times)

        Final Thought: I now know the final answer
        Final Answer: the final answer to the original input.
        '''
        """

        SUFFIX = """Begin!
        Previous conversation history:{chat_history}
        Question: {input}
        {agent_scratchpad}"""


        prompt = ZeroShotAgent.create_prompt(
            self.tools, 
            prefix=PREFIX,
            suffix=SUFFIX,
            format_instructions=FORMAT_INSTRUCTIONS,
            input_variables=["chat_history", "input", "agent_scratchpad"])
        
        def _handle_error(error) -> str:
            """If you encounter a parsing error:
            1. Review the tool's output and ensure you've extracted the necessary information.
            2. Ensure you're using the correct format for the next step.
            3. If you're unsure about the next step, refer back to the format instructions.
            4. If all else fails, restart the process and try again."""
            return str(error)[:50]

        memory = ConversationBufferWindowMemory(memory_key="chat_history", k=0)

        llm_chain = LLMChain(llm=llm, prompt=prompt)
        agent = ZeroShotAgent(
            llm_chain=llm_chain,
            tools=self.tools,
            llm=llm,
            handle_parsing_errors=True,
            max_iterations=5,
        )

        executive_agent = AgentExecutor.from_agent_and_tools(
        agent=agent,
        tools=self.tools,
        verbose=True,
        handle_parsing_errors=True,
        memory=memory,
        max_iterations=5,
        )

        return executive_agent, memory
    
    def load_memory(self, messages):
        # Skip the initial message from the assistant (index 0)
        for i in range(1, len(messages), 2):  
            user_msg = messages[i]
            if i + 1 < len(messages):  # Check if there's an assistant message after the user message
                assistant_msg = messages[i + 1]
                self.memory.save_context({"input": user_msg["content"]}, {"output": assistant_msg["content"]})  # Update memory with assistant's response
        return self.memory
    
    def get_keywords(self, llm_response):
        conversation = llm_response["chat_history"]
        keyword_list = []

        search_keywords_extract_function = {
            "name": "search_keywords_extractor",
            "description": "Creates a list of 5 short academic Google searchable keywords from the given conversation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "string",
                        "description": "List of 5 short academic Google searchable keywords"
                    }
                },
                "required": ["keywords"]
            }
        }

        res = openai.ChatCompletion.create(
            model='gpt-3.5-turbo-0613',
            messages=[{"role": "user", "content": conversation}],
            functions=[search_keywords_extract_function]
        )

        if "function_call" in res['choices'][0]['message']:
            args = json.loads(res['choices'][0]['message']['function_call']['arguments'])
            keyword_list = list(args['keywords'].split(","))

        return keyword_list

    def clear_conversation(self):
        self.memory.clear()

    def regenerate_response(self):
        st.session_state.user_input = st.session_state.history[-2].content
        st.session_state.history = st.session_state.history[:-2]
        self.run_agent()
        return
    
    def run_agent(self, input, callbacks=[]):
        # Define the logic for processing the user's input
        # For now, let's just use the agent's run method
        response = self.agent.run(input=input, callbacks=callbacks)
        return response


def main():
    load_dotenv()

    st.set_page_config(page_title="MRKL AGENT", page_icon="🦜️", layout="wide")
    st.title("🦜️ MRKL AGENT")

    if "messages" not in st.session_state:
        st.session_state.messages = [{"roles": "assistant", "content": "How can I help you?"}]
    if "user_input" not in st.session_state:
        st.session_state.user_input = None
    if "vector_store" not in st.session_state:
        st.session_state.vector_store = None
    if "summary" not in st.session_state:
        st.session_state.summary = None
    if "doc_sources" not in st.session_state:
        st.session_state.doc_sources = []
    if "history" not in st.session_state:
        st.session_state.history = []
    if "agent" not in st.session_state:
        st.session_state.agent = MRKL()
    if 'show_info' not in st.session_state:
        st.session_state.show_info = False




    st.sidebar.title("Upload Local Vector DB")
    experimental_feature = st.sidebar.toggle("Experimental Feature: Enable BR18 Database")
    st.session_state.experimental_feature = experimental_feature

    uploaded_files = st.sidebar.file_uploader("Choose a file", accept_multiple_files=True)  # You can specify the types of files you want to accept
    if uploaded_files:
        file_details = {"FileName": [], "FileType": [], "FileSize": []}

        # Populate file_details using traditional loops
        for file in uploaded_files:
            file_details["FileName"].append(file.name)
            file_details["FileType"].append(file.type)
            file_details["FileSize"].append(file.size)

        # Use selectbox to choose a file
        selected_file_name = st.sidebar.selectbox('Choose a file:', file_details["FileName"], on_change=on_selectbox_change)

        # Get the index of the file selected
        file_index = file_details["FileName"].index(selected_file_name)

        # Display details of the selected file
        st.sidebar.write("You selected:")
        st.sidebar.write("FileName : ", file_details["FileName"][file_index])
        st.sidebar.write("FileType : ", file_details["FileType"][file_index])
        st.sidebar.write("FileSize : ", file_details["FileSize"][file_index])

        # Add a note to remind the user to press the "Process" button
        if st.session_state.show_info:
            st.sidebar.info("**Note:** Remember to press the 'Process' button for the current selection.")
            st.session_state.show_info = False

        with st.sidebar:
            if st.sidebar.button("Process"):
                with st.spinner("Processing"):
                    selected_file = uploaded_files[file_index]
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmpfile:
                        tmpfile.write(selected_file.getvalue())
                        temp_path = tmpfile.name
                        db_store = DBStore(temp_path, selected_file.name)

                        document_chunks = db_store.get_pdf_text()
                        st.session_state.document_chunks = document_chunks
                        st.write(document_chunks)

                        vector_store = db_store.get_vectorstore()
                        st.session_state.vector_store = vector_store
                        st.session_state.agent = MRKL()
                        #st.write(st.session_state.document_metadata)
                        st.success("PDF uploaded successfully!")

            if "document_chunks" in st.session_state:
                    if st.sidebar.button("Create Summary"):
                        with st.spinner("Summarizing"):
                            summarization_tool = SummarizationTool(document_chunks=st.session_state.document_chunks)
                            st.session_state.summary = summarization_tool.run()
                            # Append the summary to the chat messages
                            st.session_state.messages.append({"roles": "assistant", "content": st.session_state.summary})



            


    display_messages(st.session_state.messages)

    if user_input := st.chat_input("Type something here..."):
        st.session_state.user_input = user_input
        st.session_state.messages.append({"roles": "user", "content": st.session_state.user_input})
        st.chat_message("user").write(st.session_state.user_input)

        current_user_message = {"input": st.session_state.user_input}


        with st.chat_message("assistant"):
            st_callback = StreamlitCallbackHandler(st.container(), expand_new_thoughts=False)
            response = st.session_state.agent.run_agent(input=st.session_state.user_input, callbacks=[st_callback])
            st.session_state.messages.append({"roles": "assistant", "content": response})
            st.write(response)

            current_assistant_response = {"output": response}

        current_messages = [current_user_message, current_assistant_response]    
        st.session_state.history = st.session_state.agent.load_memory(current_messages)



    with st.expander("View Document Sources"):
        st.subheader("Source Document")
        if len(st.session_state.doc_sources) != 0:

            for document in st.session_state.doc_sources:
                    st.divider()
                    source_text = f"{document.page_content}"
                    st.write(source_text)
        else:
                st.write("No document sources found")


    
    
    if st.session_state.summary is not None:
        with st.expander("Show Summary"):
            st.subheader("Summarization")
            result_summary = st.session_state.summary
            st.write(result_summary)


    buttons_placeholder = st.container()
    with buttons_placeholder:
        #st.button("Regenerate Response", key="regenerate", on_click=st.session_state.agent.regenerate_response)
        st.button("Clear Chat", key="clear", on_click=reset_chat)

    st.write(st.session_state.history)
    #st.write(st.session_state.messages)
    st.write(st.session_state.vector_store)
    st.write(st.session_state.br18_vectorstore)
    st.write(st.session_state.usc_vectorstore)




if __name__== '__main__':
    main()




    